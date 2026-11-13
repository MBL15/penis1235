"""Открытая часть REST API: чтение афиши, справочники, вопрос ИИ-помощнику. Подключается дважды: /api/v1 и /api (старые пути)."""
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from sqlalchemy import select, text

from . import config
from .agent import ask as agent_ask
from .auth import require_admin
from .config import now_local
from .deps import SessionDep
from .formatting import event_to_out
from .http_cache import cached_json
from .ingest import upsert_event
from .models import Event
from .normalize import CATEGORIES, STATUSES, canonical_category, norm_text
from .observability import metrics
from .schemas import (
    AskIn,
    AskOut,
    BulkIngestResult,
    ErrorOut,
    EventIn,
    EventList,
    EventOut,
    IngestResult,
    StatusPatch,
)
from .search import WHEN_VALUES, SearchFilters, facets, get_event, resolve_when, search
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
        q=q, city=city, category=category, date_from=date_from, date_to=date_to, price_max=price_max,
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


@router.get("/health", tags=["Служебное"], summary="Проверка работоспособности")
def health(session: SessionDep) -> dict:
    """Сервис и база отвечают. Для мониторинга; вне лимитов запросов."""
    session.execute(text("SELECT 1"))
    return {"status": "ok", "version": config.API_VERSION}


# ---------- приём данных (нужен ключ X-API-Key) ----------

@router.post("/events", response_model=IngestResult, status_code=200, tags=["Приём данных"],
             dependencies=[Depends(require_admin)], summary="Добавить событие (с дедупликацией)")
def create_event(payload: EventIn, session: SessionDep) -> IngestResult:
    event, outcome = upsert_event(session, payload)
    return IngestResult(outcome=outcome, event=event_to_out(event))


@router.post("/events/bulk", response_model=BulkIngestResult, tags=["Приём данных"],
             dependencies=[Depends(require_admin)], summary="Добавить пачку событий")
def create_events_bulk(payload: list[EventIn], session: SessionDep) -> BulkIngestResult:
    if len(payload) > 500:
        raise HTTPException(413, "За один запрос принимается не больше 500 событий")
    stats = {"created": 0, "merged": 0, "unchanged": 0}
    events: dict[int, Event] = {}
    for item in payload:
        event, outcome = upsert_event(session, item)
        stats[outcome] += 1
        events[event.id] = event
    now = now_local()
    return BulkIngestResult(**stats, events=[event_to_out(e, now) for e in events.values()])


@router.patch("/events/{event_id}/status", response_model=EventOut, tags=["Приём данных"],
              dependencies=[Depends(require_admin)], summary="Сменить статус (отмена, перенос, аншлаг)")
def patch_status(event_id: Annotated[int, Path(ge=1, le=2**31 - 1)], payload: StatusPatch, session: SessionDep) -> EventOut:
    event = get_event(session, event_id)
    if event is None:
        raise HTTPException(404, "Событие не найдено")
    event.status = payload.status
    # Ручная правка ложится на все источники: свежая выгрузка одного из них не вернёт старый статус, пока он сам не
    # пришлёт новый (источник остаётся авторитетом для собственного статуса).
    event.sources = [{**s, "status": payload.status} for s in event.sources or []]
    session.commit()
    return event_to_out(event)


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
