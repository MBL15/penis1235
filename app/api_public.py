"""Открытая часть REST API: чтение афиши, справочники, вопрос ИИ-помощнику. Подключается дважды: /api/v1 и /api (старые пути)."""
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from . import audit, config
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
from .normalize import CATEGORIES, STATUSES, canonical_category, norm_text
from .observability import metrics
from .pipeline import process_batch, process_record
from .schemas import (
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
    VenueOut,
)
from .search import WHEN_VALUES, SearchFilters, facets, get_event, resolve_when, search, venues
from .security import ask_limiter

router = APIRouter(responses={
    401: {"model": ErrorOut, "description": "Нет или неверен ключ X-API-Key"},
    404: {"model": ErrorOut, "description": "Не найдено"},
    422: {"model": ErrorOut, "description": "Некорректные данные запроса"},
    429: {"model": ErrorOut, "description": "Слишком много запросов"},
})

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
        sort=sort, limit=limit, offset=offset,
    )
    now = now_local()
    events, total = search(session, filters, now)
    page = EventList(items=[event_to_out(e, now) for e in events], total=total, limit=limit, offset=offset)
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
    result = agent_ask(session, payload.message, history)
    metrics.inc("afisha_ask_total", mode=result.mode, provider=result.provider or "none")
    now = now_local()
    return AskOut(
        reply=result.reply,
        events=[event_to_out(e, now) for e in result.events],
        needs_clarification=result.needs_clarification,
        mode=result.mode,
        provider=result.provider,
        filters=result.filters,
    )
