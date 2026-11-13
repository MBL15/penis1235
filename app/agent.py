"""ИИ-агент афиши (ТЗ §7).

Режим llm: Google Gemini (AI Studio) сам вызывает инструменты (agent_tools) по нашей базе, уточняет запрос и объясняет подборку.
Режим rules: тот же интерфейс, но запрос разбирается правилами (rules.py): работает без ключа и сети.
Для каждого ответа пишется трасса (tracing): запрос, план, вызовы инструментов, основания, задержка, токены, стоимость.
Защита (safety): инструкции из вопроса и из описаний событий не выполняются, посторонние вопросы получают короткий ответ,
действия, меняющие данные, требуют подтверждения человеком (agent_actions)."""
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime

from sqlalchemy.orm import Session

from . import agent_actions, config
from .agent_prompt import MAX_SHOWN, PROMPT_VERSION, build_system_prompt
from .agent_tools import MAX_TOOL_CALLS, TOOLS, ToolContext, filters_from_tool_input, run_tool
from .config import now_local
from .formatting import when_text
from .models import Event
from .normalize import find_city_in_text, norm_text
from .rules import parse_conversation, parse_query
from .safety import OFFTOPIC_REPLY, REFUSAL, detect_injection, is_offtopic, leaks_prompt, new_canary, ungrounded_numbers
from .search import SearchFilters, get_event, search, similar
from .security import llm_budget
from .tracing import TraceBuilder

log = logging.getLogger("afisha.agent")

MAX_STEPS = 6  # итераций цикла «модель → инструменты»; вызовов инструментов при этом не больше MAX_TOOL_CALLS
# История диалога приходит от клиента: в модель уходит только её хвост, чтобы длинной историей нельзя было
# умножить расход токенов на каждый шаг.
MAX_HISTORY_TURNS = 10
MAX_TURN_CHARS = 2000
MAX_MESSAGE_CHARS = 1000

_filters_from_tool_input = filters_from_tool_input  # прежнее имя: на него ссылаются тесты


class AgentRefusal(Exception):
    """Модель отказалась отвечать или запрос заблокирован фильтрами безопасности."""


class AgentError(Exception):
    """Агент не смог завершить ответ (слишком много шагов, битый ответ)."""


@dataclass
class AgentResult:
    reply: str
    events: list[Event] = field(default_factory=list)
    needs_clarification: bool = False
    mode: str = "rules"
    filters: dict = field(default_factory=dict)
    provider: str = ""  # gemini | "" (режим правил)
    trace_id: str = ""  # ссылка на трассу ответа
    pending_actions: list[dict] = field(default_factory=list)  # предложенные действия: ждут подтверждения человеком


# ---------- публичная точка входа ----------

def llm_enabled(client=None) -> bool:
    """Отвечает ли Gemini: режим не «rules» и есть ключ Google AI Studio (или передан готовый клиент, как в тестах)."""
    return config.AGENT_MODE != "rules" and (client is not None or bool(config.GEMINI_API_KEY))


def ask(
    session: Session,
    message: str,
    history: list[dict] | None = None,
    *,
    client=None,
    now: datetime | None = None,
    context_event_ids: list[int] | None = None,
    chat_id: int | None = None,
) -> AgentResult:
    """Ответить на вопрос пользователя. history — прошлые реплики [{role, content}, ...] без текущей.

    context_event_ids: события из прошлого ответа (для «ещё», «в другой день», «похожее»). chat_id: чат Telegram, если вопрос
    задан в боте (нужен подпискам)."""
    history = history or []
    now = now or now_local()
    message = message[:MAX_MESSAGE_CHARS]
    context_ids = [i for i in (context_event_ids or []) if isinstance(i, int)][:10]
    trace = TraceBuilder(query=message, history_turns=len(history))

    # Безопасность до любых обращений к модели: просьба раскрыть инструкции и вопрос не про афишу не стоят ни токена.
    if detect_injection(message):
        trace.flag("injection", True)
        return _finish(session, trace, AgentResult(reply=REFUSAL, mode="rules"))
    if is_offtopic(message):
        trace.flag("offtopic", True)
        return _finish(session, trace, AgentResult(reply=OFFTOPIC_REPLY, mode="rules"))

    if llm_enabled(client):
        if not llm_budget.acquire():
            trace.flag("fallback_reason", "budget")
            log.warning("Бюджет вызовов модели исчерпан (параллельность или суточный лимит), отвечаю по правилам")
        else:
            try:
                result = _gemini_ask(session, message, history, client, now, trace, context_ids, chat_id)
                result.provider = "gemini"
                return _finish(session, trace, result)
            except (AgentRefusal, AgentError) as exc:
                # Демо не должно падать из-за сети, лимитов или отказа модели — отвечаем по правилам.
                trace.flag("fallback_reason", f"{type(exc).__name__}: {str(exc)[:120]}")
                log.warning("Gemini не сработал (%s: %s), отвечаю по правилам", type(exc).__name__, exc)
            finally:
                llm_budget.release()
    return _finish(session, trace, _rules_ask(session, message, history, now, trace, context_ids, chat_id))


def _finish(session: Session, trace: TraceBuilder, result: AgentResult) -> AgentResult:
    trace.mode, trace.provider = result.mode, result.provider
    if result.mode == "rules":
        trace.model = trace.prompt_version = ""
    result.trace_id = trace.finish(session, reply=result.reply, event_ids=[e.id for e in result.events],
                                   needs_clarification=result.needs_clarification)
    return result


# ---------- общее для LLM: разбор ответа ----------

def _parse_final(text: str) -> dict | None:
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text else ""):
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and "reply" in data:
            return data
    return None


def _history_messages(history: list[dict]) -> list[dict]:
    messages = [
        {"role": "assistant" if t.get("role") == "assistant" else "user", "content": str(t["content"])[:MAX_TURN_CHARS]}
        for t in history[-MAX_HISTORY_TURNS:] if t.get("content")
    ]
    while messages and messages[0]["role"] != "user":  # диалог должен начинаться с пользователя
        messages.pop(0)
    return messages


def _result_from_final(text: str, seen: dict[int, Event], last_filters: dict) -> AgentResult:
    """Финальный ответ модели (JSON) → результат. Рекомендовать можно только события, которые вернул поиск."""
    data = _parse_final(text)
    if data is None:
        return AgentResult(reply=text or "Не удалось сформулировать ответ.", events=[], mode="llm", filters=last_filters)
    ids = []
    raw_ids = data.get("event_ids")
    for value in raw_ids if isinstance(raw_ids, list) else []:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer():
            continue
        ids.append(int(value))  # Gemini иногда присылает числа как 12.0
    shown = [seen[i] for i in dict.fromkeys(ids) if i in seen][:MAX_SHOWN]
    return AgentResult(
        reply=str(data["reply"]).strip(),
        events=shown,
        needs_clarification=bool(data.get("needs_clarification")),
        mode="llm",
        filters=last_filters,
    )


# ---------- режим llm: Google Gemini (AI Studio) ----------

_gemini: object | None = None


def _gemini_client():
    global _gemini
    if _gemini is None:
        from google import genai
        from google.genai import types

        # Таймаут в миллисекундах; прокси берётся из HTTPS_PROXY окружения (в некоторых регионах API доступен только так).
        _gemini = genai.Client(api_key=config.GEMINI_API_KEY, http_options=types.HttpOptions(timeout=30_000))
    return _gemini


def _clean_args(args: dict) -> dict:
    """Gemini присылает целые как 1000.0: возвращаем им целый вид, иначе в ответе появится «до 1000.0 ₽»."""
    return {k: int(v) if isinstance(v, float) and v.is_integer() else v for k, v in args.items()}


def _usage(response) -> tuple[int, int]:
    """Токены ответа Gemini: (входные, выходные). Размышления модели оплачиваются как выходные."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return 0, 0
    return int(getattr(usage, "prompt_token_count", 0) or 0), int((getattr(usage, "candidates_token_count", 0) or 0)
                                                                  + (getattr(usage, "thoughts_token_count", 0) or 0))


def _gemini_ask(session: Session, message: str, history: list[dict], client, now: datetime, trace: TraceBuilder,
                context_ids: list[int], chat_id: int | None) -> AgentResult:
    from google.genai import types

    client = client or _gemini_client()
    canary = new_canary()
    trace.model, trace.prompt_version = config.GEMINI_MODEL, PROMPT_VERSION
    declarations = [
        types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["input_schema"])
        for t in TOOLS
    ]
    settings = types.GenerateContentConfig(
        system_instruction=build_system_prompt(session, now, canary),
        tools=[types.Tool(function_declarations=declarations)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),  # цикл вызовов ведём сами
        max_output_tokens=4096,
        temperature=0.3,  # меньше фантазии: описываем только факты из базы
    )
    contents = [
        types.Content(role="model" if m["role"] == "assistant" else "user", parts=[types.Part.from_text(text=m["content"])])
        for m in _history_messages(history)
    ]
    hint = f"\n[Служебная справка сервера: в прошлом ответе показаны события с id {context_ids}]" if context_ids else ""
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=message + hint)]))
    ctx = ToolContext(session=session, now=now, trace=trace, chat_id=chat_id)
    ctx.source_text.append(message + " " + " ".join(str(t.get("content", "")) for t in history[-MAX_HISTORY_TURNS:]))
    deadline = time.monotonic() + config.LLM_TIME_BUDGET

    try:
        for _ in range(MAX_STEPS):
            if time.monotonic() > deadline:  # запрос не должен держать поток сервера минутами
                raise AgentError("превышено время на ответ")
            response = client.models.generate_content(model=config.GEMINI_MODEL, contents=contents, config=settings)
            trace.add_usage(*_usage(response))
            candidate = response.candidates[0] if response.candidates else None
            feedback = getattr(response, "prompt_feedback", None)
            if candidate is None or (feedback is not None and getattr(feedback, "block_reason", None)):
                raise AgentRefusal("Gemini отклонил запрос")

            calls = response.function_calls or []
            if not calls:
                reason = getattr(getattr(candidate, "finish_reason", None), "name", "")
                if reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
                    raise AgentRefusal(f"Gemini остановил ответ: {reason}")
                return _checked(_result_from_final((response.text or "").strip(), ctx.seen, ctx.last_filters), ctx, canary)

            contents.append(candidate.content)
            parts = []
            for call in calls:
                result = run_tool(ctx, call.name, _clean_args(dict(call.args or {})))
                ctx.source_text.append(json.dumps(result, ensure_ascii=False))
                payload = {"error": result["error"]} if "error" in result else {"result": result}
                parts.append(types.Part.from_function_response(name=call.name, response=payload))
            contents.append(types.Content(role="user", parts=parts))  # все результаты — одним сообщением
        raise AgentError("слишком много шагов инструментов")
    except (AgentRefusal, AgentError):
        raise
    except Exception as exc:  # сеть, лимиты, ошибки API: демо не должно падать, отвечаем по правилам
        raise AgentError(f"{type(exc).__name__}: {exc}") from exc


def _checked(result: AgentResult, ctx: ToolContext, canary: str) -> AgentResult:
    """Проверки готового ответа модели: утечка промпта, числа без оснований, предложенные действия."""
    trace = ctx.trace
    if leaks_prompt(result.reply, canary):  # метка из системного промпта появилась в ответе: промпт утёк
        trace.flag("leaked_prompt", True)
        result.reply, result.events = REFUSAL, []
    else:
        missing = ungrounded_numbers(result.reply, " ".join(ctx.source_text))
        if missing:
            trace.flag("ungrounded_numbers", missing[:10])
    trace.plan = {"filters": ctx.last_filters, "tool_calls": ctx.calls}
    result.pending_actions = ctx.pending
    if ctx.pending:
        trace.flag("pending_actions", [a["kind"] for a in ctx.pending])
    return result


# ---------- режим rules ----------

CLARIFY = (
    "Подскажите, когда и что вас интересует. Например: «в субботу с ребёнком до 1000 рублей», "
    "«бесплатные выставки в Уфе» или «концерты на выходных»."
)
_SUBSCRIPTION_WORDS = re.compile(r"\b(?:под|от)пи[сш]\w*")
_UNSUBSCRIBE_WORDS = re.compile(r"\bотпи[сш]\w*|\bотмени\w*\s+подписк")
_MOVE_WORDS = re.compile(r"\b(?:перенес\w*|перекин\w*|измени\w*|смени\w*|поменя\w*|переключ\w*)\b")
SUBSCRIPTION_HINT = (
    "Подписки на новые события оформляются в Telegram-боте: /subscribe, список подписок — /subscriptions, отписка — "
    "/unsubscribe. Через этот чат я подписки не меняю."
)


def _clear(*names: str):
    def relax(f: SearchFilters) -> None:
        for name in names:
            setattr(f, name, False if name == "free_only" else None)
    return relax


# (ключ, название для ответа, было ли условие задано, как его снять). Порядок: сначала то, что проще всего ослабить.
# Категорию отпускаем почти последней: «ближайший джаз» полезнее, чем любые события в эту субботу.
RELAXATIONS = (
    ("price", "цене", lambda f: f.price_max is not None or f.free_only, _clear("price_max", "free_only")),
    ("age", "возрасту", lambda f: f.age is not None or bool(f.kids), _clear("age", "kids")),
    ("ends", "времени окончания", lambda f: f.ends_by is not None, _clear("ends_by")),
    ("duration", "длительности", lambda f: f.max_duration is not None, _clear("max_duration")),
    ("time", "времени начала", lambda f: f.time_from is not None or f.time_to is not None, _clear("time_from", "time_to")),
    ("language", "языку", lambda f: bool(f.language), _clear("language")),
    ("date", "дате", lambda f: f.date_from is not None or f.date_to is not None, _clear("date_from", "date_to")),
    ("q", "слову", lambda f: bool(f.q), _clear("q")),
    ("category", "категории", lambda f: bool(f.category), _clear("category")),
    ("city", "городу", lambda f: bool(f.city), _clear("city")),
)
# Эти данные у событий указаны не всегда: фильтр оставляет только те, где они известны (и это надо сказать человеку)
SPARSE_KEYS = ("ends", "duration", "time", "language")


def _caveats(parsed) -> str:
    """Что мы поправили и чего не смогли учесть: молча терять условия пользователя нельзя."""
    parts = []
    if parsed.corrections:
        parts.append("Поправил опечатку: " + ", ".join(parsed.corrections) + ".")
    if parsed.unsupported:
        parts.append("Не смог учесть: " + ", ".join(parsed.unsupported) + ". Проверьте это в карточках событий.")
    return (" " + " ".join(parts)) if parts else ""


def _cancelled_note(session: Session, filters: SearchFilters, now: datetime, trace: TraceBuilder) -> str:
    """Отменённые события, подходящие под запрос. Отмена — часть задачи: о ней говорят прямо, а не молча скрывают событие."""
    found, _ = search(session, replace(filters, statuses=["cancelled"], limit=3, offset=0, exclude_ids=[]), now)
    if not found:
        return ""
    trace.step("search_events", {**filters.describe(), "statuses": ["cancelled"]}, ids=[e.id for e in found], total=len(found))
    trace.flag("cancelled_mentioned", [e.id for e in found])
    return " Отменено: " + "; ".join(f"«{e.title}» ({when_text(e)})" for e in found) + "."


def _subscription_reply(session: Session, message: str, trace: TraceBuilder, today, chat_id: int | None) -> AgentResult | None:
    """Просьбы о подписке. Данные не меняются: агент предлагает действие, выполнит его человек кнопкой."""
    if not _SUBSCRIPTION_WORDS.search(norm_text(message)):
        return None
    trace.flag("subscription_intent", True)
    if chat_id is None:
        return AgentResult(reply=SUBSCRIPTION_HINT, mode="rules")
    low = norm_text(message)
    if _UNSUBSCRIBE_WORDS.search(low):
        return AgentResult(reply="Отписаться от всех подписок можно командой /unsubscribe, список подписок: /subscriptions.", mode="rules")
    parsed = parse_query(message, today)
    if _MOVE_WORDS.search(low):
        city = parsed.filters.city
        if not city:
            return AgentResult(reply="На какой город перенести подписки? Напишите, например: «перенеси подписку на Стерлитамак».",
                               needs_clarification=True, mode="rules")
        action = agent_actions.propose(session, "move_subscription", {"chat_id": chat_id, "city": city},
                                       agent_actions.summary_move(city), trace_id=trace.id)
    else:
        category = parsed.filters.category or ""
        city = parsed.filters.city or ""
        action = agent_actions.propose(session, "subscribe", {"chat_id": chat_id, "city": city, "category": category},
                                       agent_actions.summary_subscribe(city, category), trace_id=trace.id)
    trace.step("propose_action", {"kind": action.kind, **{k: v for k, v in action.payload.items() if k != "chat_id"}})
    trace.flag("pending_actions", [action.kind])
    return AgentResult(reply=f"{action.summary} Ничего не изменено, пока вы не нажмёте «Подтвердить».", mode="rules",
                       pending_actions=[agent_actions.public(action)])


def _rules_ask(session: Session, message: str, history: list[dict], now: datetime, trace: TraceBuilder,
               context_ids: list[int], chat_id: int | None) -> AgentResult:
    today = now.date()
    user_messages = [
        str(t.get("content") or "")[:MAX_TURN_CHARS] for t in history[-2 * MAX_HISTORY_TURNS:] if t.get("role") == "user"
    ] + [message]
    subscription = _subscription_reply(session, message, trace, today, chat_id)
    if subscription is not None:
        return subscription
    parsed = parse_conversation(user_messages, today, context_ids)
    caveats = _caveats(parsed)
    trace.plan = {"conditions": parsed.notes, "corrections": parsed.corrections, "unsupported": parsed.unsupported,
                  "refinements": parsed.refinements}
    if not parsed.recognized or not parsed.intent:
        trace.flag("clarification", True)
        return AgentResult(reply=CLARIFY + caveats, needs_clarification=True, mode="rules")

    if parsed.similar_to is not None:
        liked = get_event(session, parsed.similar_to)
        if liked is not None:
            found = similar(session, liked, now, city=parsed.filters.city, date_from=parsed.filters.date_from,
                            date_to=parsed.filters.date_to, limit=MAX_SHOWN, exclude_ids=context_ids)
            trace.step("find_similar", {"event_id": liked.id, "city": parsed.filters.city}, ids=[e.id for e in found], total=len(found))
            if not found:
                return AgentResult(reply=f"Похожих на «{liked.title}» событий в афише не нашлось." + caveats, mode="rules")
            return AgentResult(reply=f"Похожее на «{liked.title}»: нашёл {len(found)} {_plural(len(found))}, показываю ближайшие."
                               + caveats, events=found, mode="rules", filters={"similar_to": liked.id})

    filters = parsed.filters
    asked_city, asked_word = bool(filters.city), filters.q
    events, total = search(session, filters, now)
    trace.step("search_events", filters.describe(), ids=[e.id for e in events], total=total)
    caveats += _cancelled_note(session, filters, now, trace)  # до ослабления условий: речь о том, что человек спросил
    # Угаданное слово — самое слабое из условий: если результатов нет, отбрасываем его первым и говорим об этом
    relaxations = sorted(RELAXATIONS, key=lambda r: r[0] != "q") if parsed.guessed_q else RELAXATIONS
    relaxed: list[tuple[str, str]] = []  # (ключ, название условия) в порядке ослабления
    blocking = False

    if not events:
        # Сначала ищем ОДНО условие, без которого что-то находится: именно оно мешает, и об этом можно сказать прямо.
        for key, label, applies, relax in relaxations:
            if not applies(filters):
                continue
            trial = replace(filters)
            relax(trial)
            found, found_total = search(session, trial, now)
            trace.step("search_events", {**trial.describe(), "relaxed": key}, ids=[e.id for e in found], total=found_total)
            if found:
                filters, events, total, relaxed, blocking = trial, found, found_total, [(key, label)], True
                break
    if not events:
        # Одного условия мало: ослабляем по очереди только те, что были заданы, и честно говорим обо всех.
        for key, label, applies, relax in relaxations:
            if events:
                break
            if not applies(filters):
                continue
            relax(filters)
            relaxed.append((key, label))
            events, total = search(session, filters, now)
            trace.step("search_events", {**filters.describe(), "relaxed": [k for k, _ in relaxed]}, ids=[e.id for e in events], total=total)

    asked = ", ".join(parsed.notes) or "без ограничений"
    if not events:
        return AgentResult(
            reply=f"По запросу ({asked}) ничего не нашлось, даже без ограничений. Попробуйте другую дату или город." + caveats,
            mode="rules", filters=filters.describe(),
        )
    shown = events[:MAX_SHOWN]
    reply = f"Нашёл {total} {_plural(total)} ({asked}). Показываю ближайшие {len(shown)}."
    if blocking and relaxed[0][0] == "q":
        reply = (
            f"По запросу ({asked}) событий со словом «{asked_word}» не нашлось. "
            f"Без этого слова нашёл {total} {_plural(total)}, показываю ближайшие {len(shown)}."
        )
    elif blocking:
        reply = (
            f"По всем условиям ({asked}) ничего нет. Ослабил условие по {relaxed[0][1]}: именно оно мешало. "
            f"Нашёл {total} {_plural(total)}, показываю ближайшие {len(shown)}."
        )
        if relaxed[0][0] in SPARSE_KEYS:
            reply += " Это условие проверяется только по событиям, где данные указаны, а у большинства событий их нет."
    elif relaxed:
        reply = (
            f"По всем условиям ({asked}) ничего нет. Ослабил условия по {', '.join(label for _, label in relaxed)}: "
            f"нашёл {total} {_plural(total)}, показываю ближайшие {len(shown)}."
        )
    if not (asked_city or relaxed):
        reply += " Город не указан — ищу по всей республике."
    return AgentResult(reply=reply + caveats, events=shown, mode="rules", filters=filters.describe())


def _plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "событие"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "события"
    return "событий"
