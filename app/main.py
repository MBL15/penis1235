"""Сборка приложения: страницы, виджет, подключение API (/api/v1 и старые пути /api/*), защита, логи и метрики."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from . import accounts, config, errors
from .api_accounts import admin_router as users_admin_router
from .api_accounts import router as accounts_router
from .api_admin import router as admin_router
from .api_public import router as public_router
from .db import init_db
from .http_cache import cached_html
from .pages import render_event_page, render_not_found_page, render_trace_page
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
* **Аккаунт** — необязателен: сайт, чат и API работают без входа. Вход только через Telegram (`/api/v1/auth/telegram`, кука `afisha_session`);
  в аккаунте история посещений (только с согласия), сохранённые поиски, подписки и обращения. Роль «Министерство» выпускает ключи для
  чтения: заголовок `X-Partner-Key`, у ключа свой лимит запросов в минуту, записать им ничего нельзя.
* **Ошибки** во всех методах в одном формате: `{{"error": {{"code", "message", "details", "request_id"}}}}`; идентификатор запроса
  всегда есть в заголовке `X-Request-ID`.
"""

TAGS = [
    {"name": "События", "description": "Поиск и карточка события, справочники для фильтров"},
    {"name": "Справочники", "description": "Города, категории и площадки для фильтров"},
    {"name": "ИИ-поиск", "description": "Запрос обычным языком"},
    {"name": "Обратная связь", "description": "Форма обращений и заявок организаторов с защитой от спама"},
    {"name": "Аккаунт", "description": "Вход через Telegram, история посещений, сохранённые поиски, ключи Министерства. Нужна кука входа"},
    {"name": "Аккаунты", "description": "Роли и ключи для встраивания: только владелец ключа администратора X-API-Key"},
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

    from .agent_actions import purge_actions
    from .tracing import purge_traces

    with SessionLocal() as session:
        purge_old(session)  # срок хранения обращений
        purge_traces(session)  # трассы ответов агента
        purge_actions(session)
        accounts.purge_history(session)  # история посещений старше срока хранения
        accounts.purge_sessions(session)  # просроченные входы
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
                   allow_headers=["Accept", "Content-Type", "If-None-Match", "X-Partner-Key"], expose_headers=["ETag", "X-Request-ID"])
if config.ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ALLOWED_HOSTS)
app.middleware("http")(guard)  # защита и заголовки безопасности
app.middleware("http")(observe)  # добавлен последним, значит выполняется первым: идентификатор запроса, лог и метрики на любой ответ

app.include_router(public_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(accounts_router, prefix="/api/v1")
app.include_router(users_admin_router, prefix="/api/v1")
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

# Страницы и их скрипты всегда сверяются с сервером: при неизменном файле это короткий ответ 304 по ETag,
# а после обновления сайта браузер сразу берёт новую версию, а не держит старый ui.js рядом с новой разметкой.
NO_CACHE = {"Cache-Control": "no-cache"}


class RevalidatedStatic(StaticFiles):
    """Статика с Cache-Control: no-cache. Без заголовка браузер сам решает, сколько хранить файл, и показывает старое."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)


@app.get("/event/{event_id}", include_in_schema=False)
def event_page(request: Request, event_id: str, session: SessionDep, user: accounts.OptionalUser) -> Response:
    """Карточка события с постоянной ссылкой. Отменённое событие открывается с явной пометкой, черновик и неверный номер дают 404.
    Если вошёл человек, согласившийся вести историю, открытие карточки попадает в «Мои посещения». Страница от входа не зависит."""
    # isascii: «²».isdigit() тоже истинно, а int("²") падает — такой адрес должен давать 404, а не 500
    valid = event_id.isascii() and event_id.isdigit() and len(event_id) <= 10 and int(event_id) < 2**31
    event = get_event(session, int(event_id)) if valid else None
    if event is None:
        return cached_html(request, render_not_found_page(), max_age=0, status_code=404)
    if user is not None:
        accounts.record_visit(session, user, event.id, "view")
    return cached_html(request, render_event_page(event), max_age=60)


@app.get("/account", include_in_schema=False)
def account_page() -> FileResponse:
    """Кабинет: вход через Telegram, история посещений, подписки, сохранённые поиски, обращения, ключи. Данные грузятся из API."""
    return FileResponse(STATIC_DIR / "account.html", headers={"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-cache"})


@app.get("/cinema", include_in_schema=False)
def cinema_page(request: Request, session: SessionDep) -> Response:
    """Кино: фильмы дня, кинотеатры и время сеансов. День и город в адресной строке, страница работает без JavaScript."""
    from .cinema_page import build_page

    return cached_html(request, build_page(session, request.query_params), max_age=60)


@app.get("/plan", include_in_schema=False)
def plan_page(request: Request, session: SessionDep) -> Response:
    """Планировщик «Выходной день»: форма и результат на одной странице; параметры в адресной строке, ссылкой можно поделиться."""
    from .plan_page import build_page

    return cached_html(request, build_page(session, request.query_params), max_age=0)


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    """Интерфейс модератора. Данные открываются только по ключу, сама страница не индексируется."""
    return FileResponse(STATIC_DIR / "admin.html", headers={"X-Robots-Tag": "noindex, nofollow", **NO_CACHE})


@app.get("/feedback", include_in_schema=False)
def feedback_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "feedback.html", headers=NO_CACHE)


@app.get("/trace/{trace_id}", include_in_schema=False)
def trace_page(request: Request, trace_id: str, session: SessionDep) -> Response:
    """Как получен ответ агента: запрос, условия, вызовы инструментов, события-основания, токены и стоимость."""
    from .api_public import trace_out
    from .tracing import get_trace

    trace = get_trace(session, trace_id)
    if trace is None:
        return cached_html(request, render_not_found_page("Трасса не найдена", "Ссылка неверна или трасса удалена: они хранятся 30 дней."),
                           max_age=0, status_code=404)
    return Response(content=render_trace_page(trace_out(session, trace)).encode("utf-8"), media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex, nofollow"})


@app.get("/widget.js", include_in_schema=False)
def widget_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "widget.js", media_type="application/javascript")


@app.get("/widget-demo", include_in_schema=False)
def widget_demo() -> FileResponse:
    return FileResponse(STATIC_DIR / "widget-demo.html", headers=NO_CACHE)


app.mount("/static", RevalidatedStatic(directory=STATIC_DIR), name="static")
