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
from .formatting import price_text, when_text
from .models import Event
from .normalize import find_city_in_text, norm_text
from .rules import parse_conversation, parse_query
from .safety import (OFFTOPIC_REPLY, REFUSAL, detect_injection, is_offtopic, leaks_prompt, new_canary, ungrounded_numbers,
                     ungrounded_titles)
from .search import SearchFilters, get_event, search, similar
from .security import llm_budget, user_llm_budget
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
    provider: str = ""  # gemini | groq | "" (режим правил)
    trace_id: str = ""  # ссылка на трассу ответа
    pending_actions: list[dict] = field(default_factory=list)  # предложенные действия: ждут подтверждения человеком
    ids_from_text: bool = False  # модель не вернула event_ids, карточки собраны по названиям из её ответа


# ---------- публичная точка входа ----------

LIMIT_NOTES = {  # что видит пользователь, когда исчерпан его лимит расхода модели (ТЗ §7.5: понятное сообщение)
    "user_daily": "ИИ-помощник на сегодня исчерпал ваш лимит, поэтому отвечаю обычным поиском по афише с теми же фильтрами. "
                  "Завтра помощник снова будет доступен.",
    "dialog": "В этом разговоре ИИ-помощник уже ответил на много вопросов, поэтому сейчас отвечаю обычным поиском по афише. "
              "Через полчаса помощник снова будет доступен.",
}

def _providers(client=None, openai_client=None) -> list[str]:
    """Провайдеры модели по порядку LLM_PROVIDERS: тот, у кого есть ключ (или передан готовый клиент, как в тестах)."""
    available = {"gemini": client is not None or bool(config.GEMINI_API_KEY),
                 "groq": openai_client is not None or bool(config.GROQ_API_KEY)}
    return [p for p in config.LLM_PROVIDERS if available.get(p)]


def llm_enabled(client=None, openai_client=None) -> bool:
    """Отвечает ли модель: режим не «rules» и есть хотя бы один провайдер с ключом."""
    return config.AGENT_MODE != "rules" and bool(_providers(client, openai_client))


def ask(
    session: Session,
    message: str,
    history: list[dict] | None = None,
    *,
    client=None,
    openai_client=None,
    now: datetime | None = None,
    context_event_ids: list[int] | None = None,
    chat_id: int | None = None,
    user_key: str | None = None,
) -> AgentResult:
    """Ответить на вопрос пользователя. history — прошлые реплики [{role, content}, ...] без текущей.

    context_event_ids: события из прошлого ответа (для «ещё», «в другой день», «похожее»). chat_id: чат Telegram, если вопрос
    задан в боте (нужен подпискам). user_key: кто спрашивает, для лимитов расхода на пользователя и диалог (ТЗ §6.5);
    по умолчанию чат Telegram."""
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
    # Историю присылает клиент, и подделать можно любую реплику, в том числе «ассистента». Реплики с инструкциями до
    # модели не доходят: иначе текущее сообщение проверялось бы, а та же просьба в «прошлой реплике» прошла бы мимо фильтра.
    clean = [t for t in history if not detect_injection(str(t.get("content") or "")[:MAX_TURN_CHARS])]
    if len(clean) != len(history):
        trace.flag("injection_in_history", len(history) - len(clean))
        history = clean

    providers = _providers(client, openai_client) if config.AGENT_MODE != "rules" else []
    if user_key is None and chat_id is not None:
        user_key = f"tg:{chat_id}"
    limit_hit = user_llm_budget.exceeded(user_key) if providers else None
    if limit_hit:
        trace.flag("fallback_reason", f"limit_{limit_hit}")
        log.info("Лимит расхода модели (%s) исчерпан, отвечаю по правилам", limit_hit)
        result = _rules_ask(session, message, history, now, trace, context_ids, chat_id)
        result.reply = f"{LIMIT_NOTES[limit_hit]}\n\n{result.reply}"
        return _finish(session, trace, result)
    if providers:
        if not llm_budget.acquire():
            trace.flag("fallback_reason", "budget")
            log.warning("Бюджет вызовов модели исчерпан (параллельность или суточный лимит), отвечаю по правилам")
        else:
            try:
                failures = []
                for name in providers:
                    try:
                        if name == "gemini":
                            result = _gemini_ask(session, message, history, client, now, trace, context_ids, chat_id)
                        else:
                            result = _openai_ask(session, message, history, openai_client, now, trace, context_ids, chat_id)
                    except AgentError as exc:  # сеть, квота, перегрузка: пробуем следующего провайдера
                        failures.append(f"{name}: {type(exc).__name__}: {str(exc)[:100]}")
                        log.warning("%s не сработал (%s: %s)", name, type(exc).__name__, exc)
                        trace.reset_attempt()
                        continue
                    result.provider = name
                    if failures:
                        trace.flag("provider_skipped", "; ".join(failures))
                    return _finish(session, trace, result)
                trace.flag("fallback_reason", "; ".join(failures))
            except AgentRefusal as exc:
                # Демо не должно падать из-за отказа модели — отвечаем по правилам.
                trace.flag("fallback_reason", f"{type(exc).__name__}: {str(exc)[:120]}")
                log.warning("Модель отказала (%s), отвечаю по правилам", exc)
            finally:
                llm_budget.release()
                user_llm_budget.record(user_key, trace.input_tokens + trace.output_tokens)
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


IDS_REMINDER = ("[Служебная проверка сервера: в ответе нет event_ids. Верни тот же JSON-ответ и укажи в event_ids id событий, "
                "которые ты называешь, только из результатов инструментов. Если подходящих событий нет, оставь event_ids пустым "
                "и прямо скажи, что их нет.]")
_NO_DATA_HINT = re.compile(r"\bнет\b|не\s+наш|не\s+найд|отсутству", re.I)


def _needs_ids(result: AgentResult, ctx: ToolContext) -> bool:
    """Модель назвала события, но не вернула их id и названия в тексте не совпали с найденными: карточек не будет, хотя
    подходящее найдено (прогон 13.11.2026: 9 запросов «пустая выдача при наличии подходящих»). Такой ответ стоит одного повтора."""
    return (not result.events and not result.needs_clarification and not ctx.pending
            and any(e.status != "cancelled" for e in ctx.seen.values()) and not _NO_DATA_HINT.search(result.reply))


def _grounding_source(message: str, history: list[dict]) -> str:
    """Что пользователь сам сказал: его числа и слова ответ может повторять. Реплики «ассистента» из истории в основу не
    берутся: историю присылает клиент, и подделанный «прошлый ответ» иначе провёл бы выдуманные цены и названия."""
    turns = [str(t.get("content", ""))[:MAX_TURN_CHARS] for t in history[-MAX_HISTORY_TURNS:] if t.get("role") != "assistant"]
    return " ".join([message, *turns])


def mentioned_events(reply: str, seen: dict[int, Event]) -> list[Event]:
    """События из результатов инструментов, названные в тексте ответа, в порядке упоминания. Отменённые не берём: их не
    рекомендуют, а об отмене говорят словами. Выдумать событие так нельзя: берутся только те, что вернул поиск."""
    haystack = f" {norm_text(reply)} "
    found: list[tuple[int, str, Event]] = []
    for event in seen.values():
        key = norm_text(event.title)
        if event.status == "cancelled" or len(key) < 4:
            continue
        position = haystack.find(f" {key} ")
        if position >= 0:
            found.append((position, key, event))
    # «Лекция» внутри «Лекция о Даля»: короткое название, которое лишь часть найденного длинного, не считаем отдельным событием
    found = [item for item in found if not any(item[1] != other[1] and item[1] in other[1] for other in found)]
    return [event for _, _, event in sorted(found, key=lambda item: item[0])]


def _result_from_final(text: str, seen: dict[int, Event], last_filters: dict) -> AgentResult:
    """Финальный ответ модели (JSON) → результат. Рекомендовать можно только события, которые вернул поиск.

    Лёгкие модели иногда отвечают обычным текстом или забывают `event_ids`, хотя события в тексте названы: тогда карточки
    собираются из названий, найденных в ответе (флаг трассы `event_ids_from_text`), чтобы подборка не осталась без карточек."""
    data = _parse_final(text)
    if data is None:
        reply = text or "Не удалось сформулировать ответ."
        shown = mentioned_events(reply, seen)[:MAX_SHOWN]
        return AgentResult(reply=reply, events=shown, mode="llm", filters=last_filters, ids_from_text=bool(shown))
    ids = []
    raw_ids = data.get("event_ids")
    for value in raw_ids if isinstance(raw_ids, list) else []:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer():
            continue
        ids.append(int(value))  # Gemini иногда присылает числа как 12.0
    shown = [seen[i] for i in dict.fromkeys(ids) if i in seen][:MAX_SHOWN]
    reply, clarifying = str(data["reply"]).strip(), bool(data.get("needs_clarification"))
    from_text = False
    if not shown and not clarifying:
        shown = mentioned_events(reply, seen)[:MAX_SHOWN]
        from_text = bool(shown)
    return AgentResult(reply=reply, events=shown, needs_clarification=clarifying, mode="llm", filters=last_filters,
                       ids_from_text=from_text)


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


_cooldown: dict[str, float] = {}  # модель -> до какого момента (monotonic) её не трогаем
SWITCH_CODES = (404, 429, 500, 502, 503, 504)  # квота, перегрузка, модель закрыта: можно попробовать другую


def _model_chain() -> list[str]:
    chain = [config.GEMINI_MODEL] + [m for m in config.GEMINI_FALLBACK_MODELS if m != config.GEMINI_MODEL]
    now = time.monotonic()
    ready = [m for m in chain if _cooldown.get(m, 0) <= now]
    return ready or chain[:1]  # все на паузе: всё равно пробуем основную, пауза не должна делать сервис немым


def _generate(client, contents, settings, trace: TraceBuilder):
    """Один вызов модели. Квота у каждой модели Gemini своя: на 429/503/404 пробуем следующую и запоминаем, что эта недоступна."""
    last: Exception | None = None
    for model in _model_chain():
        try:
            response = client.models.generate_content(model=model, contents=contents, config=settings)
        except Exception as exc:
            if getattr(exc, "code", None) not in SWITCH_CODES:
                raise
            last = exc
            _cooldown[model] = time.monotonic() + config.GEMINI_COOLDOWN_SECONDS
            trace.flag("model_skipped", ", ".join(filter(None, [str(trace.flags.get("model_skipped", "")), f"{model} ({exc.code})"])))
            log.warning("Модель %s недоступна (%s), пробую следующую", model, getattr(exc, "code", "?"))
            continue
        trace.model = model
        return response
    raise last  # type: ignore[misc]


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
    prompt = build_system_prompt(session, now, canary)
    settings = types.GenerateContentConfig(
        system_instruction=prompt,
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
    ctx.source_text.append(_grounding_source(message, history))
    deadline = time.monotonic() + config.LLM_TIME_BUDGET

    try:
        reminded = False
        for step in range(MAX_STEPS):
            if time.monotonic() > deadline:  # запрос не должен держать поток сервера минутами
                raise AgentError("превышено время на ответ")
            response = _generate(client, contents, settings, trace)
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
                result = _result_from_final((response.text or "").strip(), ctx.seen, ctx.last_filters)
                if not reminded and step + 1 < MAX_STEPS and _needs_ids(result, ctx):
                    reminded = True
                    trace.flag("ids_reminder", True)
                    contents.append(candidate.content)
                    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=IDS_REMINDER)]))
                    continue
                return _checked(result, ctx, canary, prompt)

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


# ---------- режим llm: сервисы с API в формате OpenAI (Groq) ----------

def _openai_ask(session: Session, message: str, history: list[dict], client, now: datetime, trace: TraceBuilder,
                context_ids: list[int], chat_id: int | None) -> AgentResult:
    """Тот же цикл «модель → инструменты», что у Gemini, но в формате OpenAI Chat Completions."""
    from .llm_openai import groq_client, to_openai_tools

    client = client or groq_client()
    canary = new_canary()
    model = config.GROQ_MODEL
    trace.model, trace.prompt_version = model, PROMPT_VERSION
    tools = to_openai_tools(TOOLS)
    hint = f"\n[Служебная справка сервера: в прошлом ответе показаны события с id {context_ids}]" if context_ids else ""
    prompt = build_system_prompt(session, now, canary)
    messages = [{"role": "system", "content": prompt}, *_history_messages(history),
                {"role": "user", "content": message + hint}]
    ctx = ToolContext(session=session, now=now, trace=trace, chat_id=chat_id)
    ctx.source_text.append(_grounding_source(message, history))
    deadline = time.monotonic() + config.LLM_TIME_BUDGET

    try:
        reminded = False
        for step in range(MAX_STEPS):
            if time.monotonic() > deadline:
                raise AgentError("превышено время на ответ")
            data = client.chat(model=model, messages=messages, tools=tools)
            usage = data.get("usage") or {}
            trace.add_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            choice = (data.get("choices") or [None])[0]
            if not choice:
                raise AgentRefusal("модель не вернула ответ")
            msg = choice.get("message") or {}
            calls = msg.get("tool_calls") or []
            if not calls:
                if choice.get("finish_reason") == "content_filter":
                    raise AgentRefusal("модель остановила ответ: content_filter")
                result = _result_from_final((msg.get("content") or "").strip(), ctx.seen, ctx.last_filters)
                if not reminded and step + 1 < MAX_STEPS and _needs_ids(result, ctx):
                    reminded = True
                    trace.flag("ids_reminder", True)
                    messages.append({"role": "assistant", "content": msg.get("content") or ""})
                    messages.append({"role": "user", "content": IDS_REMINDER})
                    continue
                return _checked(result, ctx, canary, prompt)

            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                function = call.get("function") or {}
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except ValueError:
                    args = {}
                result = run_tool(ctx, function.get("name", ""), _clean_args(args if isinstance(args, dict) else {}))
                ctx.source_text.append(json.dumps(result, ensure_ascii=False))
                payload = {"error": result["error"]} if "error" in result else {"result": result}
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(payload, ensure_ascii=False)})
        raise AgentError("слишком много шагов инструментов")
    except (AgentRefusal, AgentError):
        raise
    except Exception as exc:  # сеть, лимиты (429), ошибки API: отвечаем по правилам
        raise AgentError(f"{type(exc).__name__}: {exc}") from exc


def _safe_reply(events: list[Event], now: datetime) -> str:
    """Ответ только из полей базы: им заменяется текст модели, в котором нашлись факты без основания."""
    lines = [f"• «{e.title}» — {when_text(e, now)}, {e.venue or e.city}, {price_text(e)}" for e in events]
    return "По данным афиши подходят:\n" + "\n".join(lines) + "\nПодробности и ссылки на источник — в карточках."


def _checked(result: AgentResult, ctx: ToolContext, canary: str, prompt: str = "") -> AgentResult:
    """Проверки готового ответа модели: утечка промпта, факты без оснований, предложенные действия.

    Каждый факт должен браться из результатов инструментов (ТЗ §7.1). Числа (цены, даты, время, возраст) и названия
    в «ёлочках», которых там нет, пользователю не показываются: при найденных событиях текст модели заменяется ответом
    из полей базы, без событий ответ уходит в режим правил (AgentRefusal)."""
    trace = ctx.trace
    if leaks_prompt(result.reply, canary):  # метка из системного промпта появилась в ответе: промпт утёк
        trace.flag("leaked_prompt", True)
        result.reply, result.events = REFUSAL, []
    else:
        source = " ".join(ctx.source_text)
        numbers = ungrounded_numbers(result.reply, source)
        titles = ungrounded_titles(result.reply, source, prompt)
        if numbers:
            trace.flag("ungrounded_numbers", numbers[:10])
        if titles:
            trace.flag("ungrounded_titles", [t[:80] for t in titles[:10]])
        if numbers or titles:
            if not result.events or result.needs_clarification:
                raise AgentRefusal("в ответе модели факты без основания в данных")
            trace.flag("reply_replaced", True)
            result.reply = _safe_reply(result.events, ctx.now)
    if result.ids_from_text and result.events:
        trace.flag("event_ids_from_text", [e.id for e in result.events])
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
_PLAN_WORDS = re.compile(r"\b(?:сделай|составь|построй|подбери)\b.{0,30}\bплан|\bплан\s+(?:на|дня|выходн)|\bраспланир\w*")
_SUBSCRIPTION_WORDS = re.compile(r"\b(?:под|от)пи[сш]\w*")
_STATUS_ASK = re.compile(r"\bотмен\w*|\bперенес\w*|\bперенос\w*")
_UNSUBSCRIBE_WORDS =re.compile(r"\bотпи[сш]\w*|\bотмени\w*\s+подписк")
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


def _plan_reply(session: Session, message: str, now: datetime, trace: TraceBuilder) -> AgentResult | None:
    """«Сделай план на воскресенье: три события, 2000 на двоих» (ТЗ Прил. А): тот же планировщик, что у /plan и plan_day."""
    if not _PLAN_WORDS.search(norm_text(message)):
        return None
    from .planner import plan_weekend, render_plan_text, request_from_text

    request = request_from_text(message, now.date(), now=now)
    plan = plan_weekend(session, request, now)
    events = [i.event for i in plan.items]
    trace.plan = {"planner": request.describe()}
    trace.step("plan_day", {"request": "; ".join(request.describe())}, ids=[e.id for e in events], total=len(events))
    return AgentResult(reply=render_plan_text(plan), events=events, mode="rules", filters={"plan": request.describe()})


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
    plan = _plan_reply(session, message, now, trace)
    if plan is not None:
        return plan
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
    cancelled = _cancelled_note(session, filters, now, trace)  # до ослабления условий: речь о том, что человек спросил
    if not cancelled and _STATUS_ASK.search(norm_text(message)):
        # «Отменили ли спектакль?»: молчание сошло бы за «не отменили». Говорим, что именно известно, а что нет.
        cancelled = " Отменённых событий по этому запросу в афише нет, но источники сообщают об отменах не всегда: проверьте на сайте организатора."
    caveats += cancelled
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
    if blocking and relaxed[0][0] == "q" and not parsed.guessed_q and not (filters.category or filters.kids):
        # Человек прямо назвал, что ищет («джаз в Салавате»), а этого нет, и без слова от темы ничего не остаётся: честный
        # отказ без чужих карточек (ТЗ Прил. А, метрика №5). Если тема остаётся («кукольный» → детские спектакли),
        # ниже показываем близкое с оговоркой: «сначала ответ, потом предложение сузить» (§7.3).
        return AgentResult(
            reply=(f"По запросу ({asked}) событий со словом «{asked_word}» в афише нет. Без этого слова по остальным условиям "
                   f"есть {total} {_plural(total)}: напишите запрос без него, и я их покажу.") + caveats,
            mode="rules", filters=filters.describe(),
        )
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
