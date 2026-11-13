"""Сборка приложения: страницы, виджет, подключение API (/api/v1 и старые пути /api/*), защита, логи и метрики."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from . import config, errors
from .api_admin import router as admin_router
from .api_public import router as public_router
from .db import init_db
from .http_cache import cached_html
from .pages import render_event_page, render_not_found_page
from .deps import SessionDep
from .models import Event
from .observability import metrics, observe, setup_logging
from .search import SearchFilters, _conditions, get_event
from .security import ask_limiter, auth_limiter, guard  # noqa: F401  (ask_limiter и auth_limiter используют тесты)

log = logging.getLogger("afisha.api")

STATIC_DIR = config.BASE_DIR / "app" / "static"

DESCRIPTION = f"""
Единая достоверная афиша культурных событий Республики Башкортостан.

**{config.DISCLAIMER}**

* **Версия в адресе:** `/api/v1/…`. Пути без версии (`/api/events`) работают так же и оставлены для совместимости.
* **Чтение открыто** — `GET /api/v1/events`, `GET /api/v1/events/{{id}}`, справочники `/cities`, `/categories`, `/venues`: подключайте к любому сайту.
  Ответы с `ETag` и `Cache-Control`, повторный запрос с `If-None-Match` получает `304`.
* **Приём данных** — `POST /api/v1/events` с заголовком `X-API-Key`: одно и то же событие из разных источников
  автоматически склеивается в одну запись, а статус (отмена, перенос) приходит из самого тревожного источника.
* **ИИ-поиск** — `POST /api/v1/ask` принимает запрос обычным языком и возвращает подборку с объяснением.
* **Виджет** — один `<script src=".../widget.js">` на сайт, без вмешательства в его системы.
* **Ошибки** во всех методах в одном формате: `{{"error": {{"code", "message", "details", "request_id"}}}}`; идентификатор запроса
  всегда есть в заголовке `X-Request-ID`.
"""

TAGS = [
    {"name": "События", "description": "Поиск и карточка события, справочники для фильтров"},
    {"name": "Справочники", "description": "Города, категории и площадки для фильтров"},
    {"name": "ИИ-поиск", "description": "Запрос обычным языком"},
    {"name": "Обратная связь", "description": "Форма обращений и заявок организаторов с защитой от спама"},
    {"name": "Администрирование", "description": "Модерация, импорт, журнал изменений. Нужен заголовок X-API-Key; интерфейс: /admin"},
    {"name": "Приём данных", "description": "Нужен заголовок X-API-Key"},
    {"name": "Служебное", "description": "Проверка работоспособности"},
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    init_db()
    from .db import SessionLocal
    from .feedback import purge_old

    with SessionLocal() as session:
        purge_old(session)  # срок хранения обращений
    if config.ADMIN_API_KEY and len(config.ADMIN_API_KEY) < config.ADMIN_KEY_MIN_LENGTH:
        log.warning("ADMIN_API_KEY короче %d символов: приём данных отключён, задайте длинный случайный ключ",
                    config.ADMIN_KEY_MIN_LENGTH)
    yield


app = FastAPI(title="Афиша культуры Башкортостана", version=config.API_VERSION, description=DESCRIPTION,
              openapi_tags=TAGS, lifespan=lifespan)
errors.install(app)
# Чужим сайтам (виджет, сайт министерства) открыто только чтение. POST и PATCH из браузера с другого сайта
# не проходят: иначе любая страница могла бы тратить квоту ИИ руками своих посетителей.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"],
                   allow_headers=["Accept", "Content-Type", "If-None-Match"], expose_headers=["ETag", "X-Request-ID"])
if config.ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ALLOWED_HOSTS)
app.middleware("http")(guard)  # защита и заголовки безопасности
app.middleware("http")(observe)  # добавлен последним, значит выполняется первым: идентификатор запроса, лог и метрики на любой ответ

app.include_router(public_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(public_router, prefix="/api", include_in_schema=False)  # прежние адреса: виджеты и клиенты не ломаются


@app.get("/health", tags=["Служебное"], summary="Проверка, что процесс жив")
def liveness() -> dict:
    """Без обращения к базе: для проверок «жив ли процесс». Проверка вместе с базой: `/api/v1/health`."""
    return {"status": "ok", "version": config.API_VERSION}


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint(request: Request, session: SessionDep) -> PlainTextResponse:
    """Метрики Prometheus: запросы, ошибки, задержки. Закройте на прокси от внешнего мира или задайте METRICS_TOKEN."""
    if config.METRICS_TOKEN and request.headers.get("authorization", "") != f"Bearer {config.METRICS_TOKEN}":
        return errors.error_response(401, "Нужен заголовок Authorization: Bearer <токен метрик>")
    total = session.scalar(select(func.count()).select_from(Event).where(*_conditions(SearchFilters(), config.now_local()))) or 0
    return PlainTextResponse(metrics.render({"afisha_events_current": total}), media_type="text/plain; version=0.0.4")


# ---------- веб-интерфейс и виджет ----------

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/event/{event_id}", include_in_schema=False)
def event_page(request: Request, event_id: str, session: SessionDep) -> Response:
    """Карточка события с постоянной ссылкой. Отменённое событие открывается с явной пометкой, черновик и неверный номер дают 404."""
    event = get_event(session, int(event_id)) if event_id.isdigit() and len(event_id) <= 10 and int(event_id) < 2**31 else None
    if event is None:
        return cached_html(request, render_not_found_page(), max_age=0, status_code=404)
    return cached_html(request, render_event_page(event), max_age=60)


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    """Интерфейс модератора. Данные открываются только по ключу, сама страница не индексируется."""
    return FileResponse(STATIC_DIR / "admin.html", headers={"X-Robots-Tag": "noindex, nofollow"})


@app.get("/feedback", include_in_schema=False)
def feedback_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "feedback.html")


@app.get("/widget.js", include_in_schema=False)
def widget_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "widget.js", media_type="application/javascript")


@app.get("/widget-demo", include_in_schema=False)
def widget_demo() -> FileResponse:
    return FileResponse(STATIC_DIR / "widget-demo.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
