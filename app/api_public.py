"""Открытая часть REST API: чтение афиши, справочники, вопрос ИИ-помощнику. Подключается дважды: /api/v1 и /api (старые пути)."""
from datetime import date, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from . import audit, config
from . import agent_actions
from .agent import ask as agent_ask
from .auth import require_admin
from .config import now_local
from .feedback import (
    KINDS,
    MIN_SECONDS,
    FeedbackAccepted,
    FeedbackIn,
    check_content,
    check_token,
    feedback_global_limiter,
    feedback_limiter,
    issue_token,
    purge_old,
    save_feedback,
)
from .deps import SessionDep
from .formatting import event_to_out
from .http_cache import cached_json
from .models import Event
from .normalize import CATEGORIES, STATUSES, canonical_category, canonical_city, norm_text
from .observability import metrics
from .pipeline import process_batch, process_record
from .schemas import (
    ActionResult,
    AskIn,
    AskOut,
    BulkIngestResult,
    CategoryOut,
    CityOut,
    ErrorOut,
    EventIn,
    EventList,
    EventOut,
    IngestResult,
    RejectedRecord,
    StatusPatch,
    PlanIn,
    PlanItemOut,
    PlanOut,
    SubscriptionIn,
    SubscriptionOut,
    TraceOut,
    VenueOut,
)
from .tracing import get_trace
from .hints import empty_hint
from .planner import PlanRequest, plan_weekend
from .search import WHEN_VALUES, SearchFilters, facets, get_event, resolve_when, search, venues
from .security import RateLimiter, ask_limiter
from .telegram_auth import validate_init_data
from . import subscriptions

router = APIRouter(responses={
    401: {"model": ErrorOut, "description": "Нет или неверен ключ X-API-Key"},
    404: {"model": ErrorOut, "description": "Не найдено"},
    422: {"model": ErrorOut, "description": "Некорректные данные запроса"},
    429: {"model": ErrorOut, "description": "Слишком много запросов"},
})

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


def _hhmm(value: str | None) -> time | None:
    return time(int(value[:2]), int(value[3:])) if value else None


# Сколько секунд ответ можно держать в кеше браузера и прокси (ETag работает всегда).
TTL_EVENT, TTL_LIST, TTL_DIRECTORY = 60, 30, 300


@router.get("/events", response_model=EventList, summary="Поиск событий", tags=["События"])
def list_events(
    request: Request,
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=200, description="Слова из названия, описания, площадки")] = None,
    city: Annotated[str | None, Query(max_length=64, description="Например, Уфа")] = None,
    venue: Annotated[str | None, Query(max_length=200, description="Площадка: часть названия, список в /venues")] = None,
    category: Annotated[str | None, Query(max_length=200, description=f"Один или несколько через запятую: {', '.join(CATEGORIES)}")] = None,
    when: Annotated[str | None, Query(max_length=16, description="today | tomorrow | weekend | week")] = None,
    date_from: Annotated[date | None, Query(description="С даты (включительно), ГГГГ-ММ-ДД")] = None,
    date_to: Annotated[date | None, Query(description="По дату (включительно)")] = None,
    price_max: Annotated[int | None, Query(ge=0, description="Не дороже, ₽ (события без цены не попадают)")] = None,
    free: Annotated[bool, Query(description="Только бесплатные")] = False,
    age: Annotated[int | None, Query(ge=0, le=99, description="Возраст посетителя: покажем события с ограничением не выше")] = None,
    time_from: Annotated[str | None, Query(pattern=HHMM, description="Начало не раньше, ЧЧ:ММ. События без указанного времени не подходят")] = None,
    time_to: Annotated[str | None, Query(pattern=HHMM, description="Начало не позже, ЧЧ:ММ")] = None,
    ends_by: Annotated[str | None, Query(pattern=HHMM, description="Закончится не позже, ЧЧ:ММ. Только у событий с известной длительностью")] = None,
    max_duration: Annotated[int | None, Query(ge=1, le=1440, description="Длительность не больше, минут. Только у событий с известной длительностью")] = None,
    language: Annotated[str | None, Query(max_length=32, description="Язык мероприятия: ba, ru, tt, en. Только у событий, где язык указан")] = None,
    kids: Annotated[bool | None, Query(description="Только детские и семейные")] = None,
    status: Annotated[list[str] | None, Query(max_length=8, description=f"{', '.join(STATUSES)} или all. По умолчанию без отменённых")] = None,
    include_past: Annotated[bool, Query(description="Включить уже прошедшие")] = False,
    sort: Annotated[str, Query(pattern="^(date|price)$")] = "date",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> Response:
    if when is not None and when not in WHEN_VALUES:
        raise HTTPException(422, f"when должен быть одним из: {', '.join(WHEN_VALUES)}")
    if when and not (date_from or date_to):
        date_from, date_to = resolve_when(when, now_local().date())
    if date_from and date_to and date_to < date_from:
        raise HTTPException(422, "date_to раньше date_from")
    bad = [s for s in (status or []) if s != "all" and s not in STATUSES]
    if bad:
        raise HTTPException(422, f"Неизвестный статус: {', '.join(bad)}")
    # canonical_category отдаёт «other» для чего угодно непонятного: опечатку в фильтре не прячем за «Другим».
    unknown = [
        c.strip() for c in (category or "").split(",")
        if c.strip() and canonical_category(c) == "other" and norm_text(c) not in ("other", "другое")
    ]
    if unknown:
        raise HTTPException(422, f"Неизвестная категория: {', '.join(unknown)}. Допустимые: {', '.join(CATEGORIES)}")

    filters = SearchFilters(
        q=q, city=city, venue=venue, category=category, date_from=date_from, date_to=date_to, price_max=price_max,
        free_only=free, age=age, kids=kids, statuses=status, include_past=include_past,
        time_from=_hhmm(time_from), time_to=_hhmm(time_to), ends_by=_hhmm(ends_by), max_duration=max_duration, language=language,
        sort=sort, limit=limit, offset=offset,
    )
    now = now_local()
    events, total = search(session, filters, now)
    page = EventList(items=[event_to_out(e, now) for e in events], total=total, limit=limit, offset=offset,
                     hint=empty_hint(session, filters, now) if total == 0 and offset == 0 else None)
    return cached_json(request, page, max_age=TTL_LIST)


@router.get("/events/{event_id}", response_model=EventOut, summary="Карточка события", tags=["События"])
def read_event(request: Request, event_id: Annotated[int, Path(ge=1, le=2**31 - 1)], session: SessionDep) -> Response:
    """Отменённое событие тоже доступно по прямой ссылке: у него `status` = `cancelled` и явная пометка в `status_label`."""
    event = get_event(session, event_id)
    if event is None:
        raise HTTPException(404, "Событие не найдено")
    return cached_json(request, event_to_out(event), max_age=TTL_EVENT)


@router.get("/meta", summary="Справочники для фильтров", tags=["События"])
def meta(request: Request, session: SessionDep) -> Response:
    data = facets(session)
    counts = {c["slug"]: c["count"] for c in data["categories"]}
    has_demo = any(
        all(str(s.get("name", "")).startswith("demo") for s in (e.sources or [])) and e.sources
        for e in session.scalars(select(Event).limit(500))
    )
    payload = {
        "cities": data["cities"],
        "categories": [{"slug": s, "label": label, "count": counts.get(s, 0)} for s, label in CATEGORIES.items()],
        "statuses": [{"slug": s, "label": label} for s, label in STATUSES.items()],
        "demo_data": has_demo,
        "disclaimer": config.DISCLAIMER,
    }
    return cached_json(request, payload, max_age=TTL_DIRECTORY)


@router.get("/cities", response_model=list[CityOut], summary="Города с числом событий", tags=["Справочники"])
def cities(request: Request, session: SessionDep) -> Response:
    return cached_json(request, facets(session)["cities"], max_age=TTL_DIRECTORY)


@router.get("/categories", response_model=list[CategoryOut], summary="Категории с числом событий", tags=["Справочники"])
def categories(request: Request, session: SessionDep) -> Response:
    counts = {c["slug"]: c["count"] for c in facets(session)["categories"]}
    return cached_json(request, [{"slug": s, "label": label, "count": counts.get(s, 0)} for s, label in CATEGORIES.items()],
                       max_age=TTL_DIRECTORY)


@router.get("/venues", response_model=list[VenueOut], summary="Площадки с числом событий", tags=["Справочники"])
def venue_directory(
    request: Request,
    session: SessionDep,
    city: Annotated[str | None, Query(max_length=64, description="Только площадки этого города")] = None,
    q: Annotated[str | None, Query(max_length=200, description="Часть названия площадки")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> Response:
    """Название из этого списка передаётся в `GET /events?venue=`. Написания одной площадки в разных источниках объединены."""
    return cached_json(request, venues(session, city=city, q=q, limit=limit), max_age=TTL_DIRECTORY)


@router.get("/health", tags=["Служебное"], summary="Проверка работоспособности")
def health(session: SessionDep) -> dict:
    """Сервис и база отвечают. Для мониторинга; вне лимитов запросов."""
    session.execute(text("SELECT 1"))
    return {"status": "ok", "version": config.API_VERSION}


# ---------- приём данных (нужен ключ X-API-Key) ----------

@router.post("/events", response_model=IngestResult, status_code=200, tags=["Приём данных"],
             summary="Добавить событие (с дедупликацией)")
def create_event(payload: EventIn, session: SessionDep, actor: Annotated[str, Depends(require_admin)]) -> IngestResult:
    """Одно и то же событие из разных источников склеивается в одну запись. Правка попадает в журнал изменений."""
    result = process_record(session, payload, actor=actor)
    return IngestResult(outcome=result.status, event=event_to_out(result.event))


@router.post("/events/bulk", response_model=BulkIngestResult, tags=["Приём данных"], summary="Добавить пачку событий")
def create_events_bulk(
    payload: Annotated[list[Any], Body(description="Список событий в формате EventIn (до 500 за запрос)")],
    session: SessionDep,
    actor: Annotated[str, Depends(require_admin)],
    publish: Annotated[bool, Query(description="false: новые события сохраняются черновиками для модерации")] = True,
) -> BulkIngestResult:
    """Плохая запись не отклоняет всю пачку: в ответе `rejected` перечисляет отклонённые записи и причины по-русски."""
    if len(payload) > 500:
        raise HTTPException(413, "За один запрос принимается не больше 500 событий")
    report = process_batch(session, payload, actor=actor, publish=publish)
    ids = list(dict.fromkeys(report.event_ids))
    events = {e.id: e for e in session.scalars(select(Event).where(Event.id.in_(ids)))} if ids else {}
    now = now_local()
    return BulkIngestResult(
        created=report.created, updated=report.updated, merged=report.updated, unchanged=report.unchanged,
        rejected=[RejectedRecord(row=r.row, title=r.title, reasons=r.reasons) for r in report.rejected],
        total=report.total, events=[event_to_out(events[i], now) for i in ids if i in events],
    )


@router.patch("/events/{event_id}/status", response_model=EventOut, tags=["Приём данных"],
              summary="Сменить статус (отмена, перенос, аншлаг)")
def patch_status(event_id: Annotated[int, Path(ge=1, le=2**31 - 1)], payload: StatusPatch, session: SessionDep,
                 actor: Annotated[str, Depends(require_admin)]) -> EventOut:
    event = get_event(session, event_id, include_drafts=True)
    if event is None:
        raise HTTPException(404, "Событие не найдено")
    before = audit.snapshot(event)
    event.status = payload.status
    # Ручная правка ложится на все источники: свежая выгрузка одного из них не вернёт старый статус, пока он сам не
    # пришлёт новый (источник остаётся авторитетом для собственного статуса).
    event.sources = [{**s, "status": payload.status} for s in event.sources or []]
    audit.record(session, actor, "status", event.id, audit.diff(before, audit.snapshot(event)))
    session.commit()
    return event_to_out(event)


# ---------- планировщик «Выходной день» (ТЗ §6.5) ----------

@router.post("/plan", response_model=PlanOut, tags=["Планировщик"], summary="План дня из 2–4 событий",
             responses={422: {"model": ErrorOut}})
def plan_day(payload: PlanIn, session: SessionDep) -> PlanOut:
    """Собирает из афиши 2–4 события одного дня без пересечений по времени: с оценкой дороги и общей стоимостью. Если ограничения
    несовместимы, `feasible` = false, а `message` и `suggestions` говорят, что мешает и что изменить. Модель не вызывается."""
    today = now_local().date()
    if not today <= payload.day <= today + timedelta(days=90):
        raise HTTPException(422, "Планировать можно на день от сегодняшнего до 90 дней вперёд")
    city = canonical_city(payload.city) if payload.city else None
    if payload.city and (not city or city not in {c["name"] for c in facets(session)["cities"]}):
        raise HTTPException(422, f"Такого города нет в афише: «{payload.city[:40]}». Список: GET /api/v1/cities")
    category = canonical_category(payload.category) if payload.category else None
    if payload.category and category == "other" and norm_text(payload.category) not in ("other", "другое"):
        raise HTTPException(422, f"Неизвестная категория: {payload.category[:40]}. Допустимые: {', '.join(CATEGORIES)}")
    request = PlanRequest(
        day=payload.day, window_from=_hhmm(payload.window_from), window_to=_hhmm(payload.window_to), budget=payload.budget,
        adults=payload.adults, children=payload.children, city=city, max_travel_min=payload.max_travel_min,
        events=payload.events, category=category)
    if request.window_from >= request.window_to:
        raise HTTPException(422, "Окно времени: «с» должно быть раньше «по»")
    now = now_local()
    result = plan_weekend(session, request, now)
    return PlanOut(
        feasible=result.feasible, message=result.message, suggestions=result.suggestions, constraints=request.describe(),
        requested_events=request.events, total_cost=result.total_cost, total_travel_min=result.total_travel_min,
        items=[PlanItemOut(event=event_to_out(i.event, now), starts_at=i.start, ends_at=i.end, end_estimated=i.end_estimated,
                           travel_min=i.travel_min, cost=i.cost, price_known=i.price_known) for i in result.items])


# ---------- подписки (ТЗ §6.3) ----------

def telegram_chat(request: Request) -> int:
    """Чей это чат: из подписи Mini App в заголовке X-Telegram-Init-Data. Номер чата на веру не принимается."""
    if not config.TELEGRAM_BOT_TOKEN:
        raise HTTPException(503, "Подписки через API работают из мини-приложения Telegram; на этом сервере они не настроены. "
                                 "Оформите подписку в боте: /subscribe")
    chat_id = validate_init_data(request.headers.get("x-telegram-init-data", ""), config.TELEGRAM_BOT_TOKEN)
    if chat_id is None:
        raise HTTPException(401, "Нужна подпись Telegram: откройте афишу из бота (заголовок X-Telegram-Init-Data)")
    return chat_id


ChatDep = Annotated[int, Depends(telegram_chat)]
subscription_limiter = RateLimiter(limit=30, detail="Слишком много действий с подписками, попробуйте через минуту")


def _sub_out(sub) -> SubscriptionOut:
    return SubscriptionOut(id=sub.id, city=sub.city, category=sub.category, label=subscriptions.describe(sub))


@router.get("/subscriptions", response_model=list[SubscriptionOut], tags=["Подписки"], summary="Мои подписки",
            dependencies=[Depends(subscription_limiter)], responses={401: {"model": ErrorOut}, 503: {"model": ErrorOut}})
def list_subscriptions(chat_id: ChatDep, session: SessionDep) -> list[SubscriptionOut]:
    """Подписки того чата, который подписал запрос. Чужие подписки увидеть нельзя. Хранятся номер чата и фильтры, срок 12 месяцев."""
    return [_sub_out(s) for s in subscriptions.list_for(session, chat_id)]


@router.post("/subscriptions", response_model=SubscriptionOut, status_code=201, tags=["Подписки"], summary="Подписаться на город и категорию",
             dependencies=[Depends(subscription_limiter)], responses={401: {"model": ErrorOut}, 409: {"model": ErrorOut}, 422: {"model": ErrorOut}})
def create_subscription(payload: SubscriptionIn, chat_id: ChatDep, session: SessionDep) -> SubscriptionOut:
    """Уведомления о новых событиях приходят в Telegram не чаще раза в сутки, с 9 до 21 часа."""
    city = canonical_city(payload.city) if payload.city else ""
    if payload.city and (not city or city not in {c["name"] for c in facets(session)["cities"]}):
        raise HTTPException(422, f"Такого города нет в афише: «{payload.city[:40]}». Список: GET /api/v1/cities")
    category = canonical_category(payload.category) if payload.category else ""
    if payload.category and category == "other" and norm_text(payload.category) not in ("other", "другое"):
        raise HTTPException(422, f"Неизвестная категория: {payload.category[:40]}. Допустимые: {', '.join(CATEGORIES)}")
    outcome = subscriptions.add(session, chat_id, city, category)
    if outcome == "limit":
        raise HTTPException(409, f"Уже {subscriptions.MAX_PER_CHAT} подписок: удалите ненужную")
    if outcome == "exists":
        raise HTTPException(409, "Такая подписка уже есть")
    sub = next(s for s in subscriptions.list_for(session, chat_id) if s.city == city and s.category == category)
    return _sub_out(sub)


@router.delete("/subscriptions/{sub_id}", status_code=204, tags=["Подписки"], summary="Отписаться от одной подписки",
               dependencies=[Depends(subscription_limiter)], responses={401: {"model": ErrorOut}, 404: {"model": ErrorOut}})
def delete_subscription(sub_id: Annotated[int, Path(ge=1, le=2**31 - 1)], chat_id: ChatDep, session: SessionDep) -> Response:
    if not subscriptions.remove(session, chat_id, sub_id):
        raise HTTPException(404, "Подписка не найдена")
    return Response(status_code=204)


@router.delete("/subscriptions", tags=["Подписки"], summary="Отписаться от всего одним вызовом",
               dependencies=[Depends(subscription_limiter)], responses={401: {"model": ErrorOut}})
def delete_all_subscriptions(chat_id: ChatDep, session: SessionDep) -> dict:
    return {"removed": subscriptions.remove_all(session, chat_id)}


# ---------- обратная связь ----------

@router.get("/feedback/form", tags=["Обратная связь"], summary="Метка формы обратной связи")
def feedback_form() -> JSONResponse:
    """Подписанная серверная метка времени: форму нельзя отправить мгновенно, как это делают роботы. Не кешируется."""
    return JSONResponse({"token": issue_token(), "min_seconds": MIN_SECONDS,
                         "kinds": [{"slug": slug, "label": label} for slug, label in KINDS.items()]},
                        headers={"Cache-Control": "no-store"})


@router.post("/feedback", response_model=FeedbackAccepted, tags=["Обратная связь"], dependencies=[Depends(feedback_limiter)],
             summary="Отправить обращение или заявку организатора")
def send_feedback(payload: FeedbackIn, session: SessionDep) -> FeedbackAccepted:
    """Защита от спама: поле-ловушка `website`, метка времени формы, лимит с одного адреса, не больше двух ссылок, отсев повторов.
    Контакт необязателен. Обращения хранятся не дольше 180 дней (обработанные 90)."""
    thanks = FeedbackAccepted(message="Спасибо! Обращение принято, модератор рассмотрит его.")
    if payload.website.strip():  # роботы заполняют все поля подряд: делаем вид, что приняли, и ничего не сохраняем
        metrics.inc("afisha_feedback_total", result="honeypot")
        return thanks
    reason = check_token(payload.token)
    if reason:
        metrics.inc("afisha_feedback_total", result="rejected_form")
        raise HTTPException(422, reason)
    if not feedback_global_limiter.allow("all"):
        raise HTTPException(429, feedback_global_limiter.detail, headers={"Retry-After": "3600"})
    reason = check_content(payload.message)
    if reason:
        metrics.inc("afisha_feedback_total", result="rejected_content")
        raise HTTPException(422, reason)
    if payload.event_id is not None and get_event(session, payload.event_id) is None:
        raise HTTPException(422, "Такого события нет: проверьте номер или уберите его")
    purge_old(session)
    _, result = save_feedback(session, payload)
    metrics.inc("afisha_feedback_total", result=result)
    return thanks


# ---------- ИИ-поиск ----------

@router.post("/ask", response_model=AskOut, tags=["ИИ-поиск"], dependencies=[Depends(ask_limiter)],
             summary="Вопрос обычным языком → подборка событий")
def ask(payload: AskIn, session: SessionDep) -> AskOut:
    history = [turn.model_dump() for turn in payload.history]
    result = agent_ask(session, payload.message, history, context_event_ids=payload.context_event_ids)
    metrics.inc("afisha_ask_total", mode=result.mode, provider=result.provider or "none")
    for flag in ("injection", "offtopic"):
        if flag in _trace_flags(session, result.trace_id):
            metrics.inc("afisha_agent_blocked_total", reason=flag)
    now = now_local()
    return AskOut(
        reply=result.reply,
        events=[event_to_out(e, now) for e in result.events],
        needs_clarification=result.needs_clarification,
        mode=result.mode,
        provider=result.provider,
        filters=result.filters,
        trace_id=result.trace_id,
        pending_actions=result.pending_actions,
    )


def _trace_flags(session, trace_id: str) -> dict:
    trace = get_trace(session, trace_id)
    return (trace.flags or {}) if trace is not None else {}


def trace_out(session, trace) -> TraceOut:
    """Трасса для API и страницы: события-основания с названиями, стоимость с пометкой, если тариф не задан."""
    titles = {e.id: e.title for e in session.scalars(select(Event).where(Event.id.in_(trace.event_ids or [])))} if trace.event_ids else {}
    note = ""
    if trace.mode == "llm" and trace.cost_rub is None:
        note = "Тариф модели не задан (LLM_PRICE_INPUT_RUB_PER_M, LLM_PRICE_OUTPUT_RUB_PER_M): стоимость не посчитана, токены учтены."
    return TraceOut(
        id=trace.id, created_at=trace.created_at, mode=trace.mode, provider=trace.provider, model=trace.model,
        prompt_version=trace.prompt_version, query=trace.query, history_turns=trace.history_turns, plan=trace.plan or {},
        steps=trace.steps or [], event_ids=trace.event_ids or [], events=[{"id": i, "title": titles.get(i, "")} for i in trace.event_ids or []],
        reply=trace.reply, needs_clarification=trace.needs_clarification, flags=trace.flags or {}, latency_ms=trace.latency_ms,
        input_tokens=trace.input_tokens, output_tokens=trace.output_tokens, cost_rub=trace.cost_rub, cost_note=note,
    )


@router.get("/traces/{trace_id}", response_model=TraceOut, tags=["ИИ-поиск"], summary="Трасса ответа агента",
            description="По ссылке из ответа `/ask`: запрос, как он понят, какие инструменты вызваны с какими аргументами, на каких "
                        "событиях основан ответ, задержка, токены и стоимость. Идентификатор случайный: это и есть ссылка. Хранится 30 дней.")
def read_trace(trace_id: Annotated[str, Path(min_length=32, max_length=32, pattern="^[0-9a-f]{32}$")], session: SessionDep) -> TraceOut:
    trace = get_trace(session, trace_id)
    if trace is None:
        raise HTTPException(404, "Трасса не найдена или уже удалена (хранится 30 дней)")
    return trace_out(session, trace)


@router.post("/agent/actions/{action_id}/confirm", response_model=ActionResult, tags=["ИИ-поиск"], dependencies=[Depends(ask_limiter)],
             summary="Подтвердить действие, которое предложил агент")
def confirm_action(action_id: Annotated[str, Path(min_length=32, max_length=32, pattern="^[0-9a-f]{32}$")], session: SessionDep) -> ActionResult:
    """Действие выполняется только здесь, по явному подтверждению человека. Подписки привязаны к чату Telegram и подтверждаются в боте."""
    ok, message = agent_actions.confirm(session, action_id)
    return ActionResult(ok=ok, message=message)


@router.post("/agent/actions/{action_id}/cancel", response_model=ActionResult, tags=["ИИ-поиск"], dependencies=[Depends(ask_limiter)],
             summary="Отказаться от предложенного агентом действия")
def cancel_action(action_id: Annotated[str, Path(min_length=32, max_length=32, pattern="^[0-9a-f]{32}$")], session: SessionDep) -> ActionResult:
    done = agent_actions.cancel(session, action_id)
    return ActionResult(ok=done, message="Действие отменено." if done else "Такого действия нет, оно уже обработано или его нельзя отменить здесь.")
