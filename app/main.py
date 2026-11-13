"""REST API, веб-интерфейс и встраиваемый виджет."""
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .agent import ask as agent_ask
from .db import get_session, init_db
from .formatting import event_to_out
from .models import Event
from .normalize import CATEGORIES, STATUSES, canonical_category, norm_text
from .schemas import (
    AskIn,
    AskOut,
    BulkIngestResult,
    EventIn,
    EventList,
    EventOut,
    IngestResult,
    StatusPatch,
)
from .ingest import upsert_event
from .search import WHEN_VALUES, SearchFilters, facets, get_event, resolve_when, search
from .security import RateLimiter, ask_limiter, auth_limiter, guard
from .config import now_local

log = logging.getLogger("afisha.api")

STATIC_DIR = config.BASE_DIR / "app" / "static"

DESCRIPTION = """
Единая достоверная афиша культурных событий Республики Башкортостан.

* **Чтение открыто** — `GET /api/events`, `GET /api/events/{id}`, `GET /api/meta`: подключайте к любому сайту.
* **Приём данных** — `POST /api/events` с заголовком `X-API-Key`: одно и то же событие из разных источников
  автоматически склеивается в одну запись, а статус (отмена, перенос) приходит из самого тревожного источника.
* **ИИ-поиск** — `POST /api/ask` принимает запрос обычным языком и возвращает подборку с объяснением.
* **Виджет** — один `<script src=".../widget.js">` на сайт, без вмешательства в его системы.
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if config.ADMIN_API_KEY and len(config.ADMIN_API_KEY) < config.ADMIN_KEY_MIN_LENGTH:
        log.warning("ADMIN_API_KEY короче %d символов: приём данных отключён, задайте длинный случайный ключ",
                    config.ADMIN_KEY_MIN_LENGTH)
    yield


app = FastAPI(title="Афиша культуры Башкортостана", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)
# Чужим сайтам (виджет, сайт министерства) открыто только чтение. POST и PATCH из браузера с другого сайта
# не проходят: иначе любая страница могла бы тратить квоту ИИ руками своих посетителей.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["Accept", "Content-Type"])
if config.ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ALLOWED_HOSTS)
app.middleware("http")(guard)  # добавлен последним, значит выполняется первым и ставит заголовки на любой ответ

SessionDep = Annotated[Session, Depends(get_session)]


# ---------- защита ----------

def require_admin(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> None:
    key = config.ADMIN_API_KEY
    if not key:
        raise HTTPException(503, "Приём данных отключён: на сервере не задан ADMIN_API_KEY")
    if len(key) < config.ADMIN_KEY_MIN_LENGTH:
        raise HTTPException(503, f"Приём данных отключён: ADMIN_API_KEY короче {config.ADMIN_KEY_MIN_LENGTH} символов")
    ip = RateLimiter.client_ip(request)
    if auth_limiter.blocked(ip):  # подбор ключа: после 10 неудач с адреса минуту не принимаем даже верный ключ
        raise HTTPException(429, auth_limiter.detail, headers={"Retry-After": "60"})
    # Сравниваем байты: compare_digest на строках с не-ASCII символами бросает TypeError.
    if not x_api_key or not secrets.compare_digest(x_api_key.encode("utf-8"), key.encode("utf-8")):
        auth_limiter.allow(ip)
        raise HTTPException(401, "Неверный или отсутствующий заголовок X-API-Key")


# ---------- чтение ----------

@app.get("/api/events", response_model=EventList, summary="Поиск событий", tags=["События"])
def list_events(
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
) -> EventList:
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
    return EventList(items=[event_to_out(e, now) for e in events], total=total, limit=limit, offset=offset)


@app.get("/api/events/{event_id}", response_model=EventOut, summary="Карточка события", tags=["События"])
def read_event(event_id: Annotated[int, Path(ge=1, le=2**31 - 1)], session: SessionDep) -> EventOut:
    event = get_event(session, event_id)
    if event is None:
        raise HTTPException(404, "Событие не найдено")
    return event_to_out(event)


@app.get("/api/meta", summary="Справочники для фильтров", tags=["События"])
def meta(session: SessionDep) -> dict:
    data = facets(session)
    counts = {c["slug"]: c["count"] for c in data["categories"]}
    has_demo = any(
        all(str(s.get("name", "")).startswith("demo") for s in (e.sources or [])) and e.sources
        for e in session.scalars(select(Event).limit(500))
    )
    return {
        "cities": data["cities"],
        "categories": [{"slug": s, "label": label, "count": counts.get(s, 0)} for s, label in CATEGORIES.items()],
        "statuses": [{"slug": s, "label": label} for s, label in STATUSES.items()],
        "demo_data": has_demo,
        "now": now_local().isoformat(timespec="minutes"),
    }


@app.get("/api/health", include_in_schema=False)
def health() -> dict:
    return {"ok": True}


# ---------- приём данных ----------

@app.post("/api/events", response_model=IngestResult, status_code=200, tags=["Приём данных"],
          dependencies=[Depends(require_admin)], summary="Добавить событие (с дедупликацией)")
def create_event(payload: EventIn, session: SessionDep) -> IngestResult:
    event, outcome = upsert_event(session, payload)
    return IngestResult(outcome=outcome, event=event_to_out(event))


@app.post("/api/events/bulk", response_model=BulkIngestResult, tags=["Приём данных"],
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


@app.patch("/api/events/{event_id}/status", response_model=EventOut, tags=["Приём данных"],
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

@app.post("/api/ask", response_model=AskOut, tags=["ИИ-поиск"], dependencies=[Depends(ask_limiter)],
          summary="Вопрос обычным языком → подборка событий")
def ask(payload: AskIn, session: SessionDep) -> AskOut:
    history = [turn.model_dump() for turn in payload.history]
    result = agent_ask(session, payload.message, history)
    now = now_local()
    return AskOut(
        reply=result.reply,
        events=[event_to_out(e, now) for e in result.events],
        needs_clarification=result.needs_clarification,
        mode=result.mode,
        provider=result.provider,
        filters=result.filters,
    )


# ---------- веб-интерфейс и виджет ----------

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/widget.js", include_in_schema=False)
def widget_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "widget.js", media_type="application/javascript")


@app.get("/widget-demo", include_in_schema=False)
def widget_demo() -> FileResponse:
    return FileResponse(STATIC_DIR / "widget-demo.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
