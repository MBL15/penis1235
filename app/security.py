"""Защита HTTP-слоя: лимиты запросов, заголовки безопасности, размер тела запроса, запрет чужих POST, бюджет вызовов ИИ."""
import ipaddress
import logging
import threading
import time
from collections import defaultdict, deque
from datetime import date
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from . import config
from .errors import error_response

log = logging.getLogger("afisha.security")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_BODY = 1024 * 1024  # 1 МБ хватает любому JSON-запросу интерфейса
MAX_BODY_BULK = 5 * 1024 * 1024  # пачка до 500 событий
BULK_PATHS = ("/api/events/bulk", "/api/v1/events/bulk", "/api/v1/admin/import")
HEALTH_PATHS = ("/api/health", "/api/v1/health")


# ---------- адрес посетителя и лимиты ----------

def client_key(host: str) -> str:
    """Ключ лимита по адресу. IPv6 сводим к подсети /64: её целиком выдают одному абоненту, и без этого лимит
    обходится простой сменой адреса внутри своей подсети."""
    try:
        ip = ipaddress.ip_address(host.strip())
    except ValueError:
        return host[:64] or "unknown"
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return f"{ipaddress.ip_network((ip, 64), strict=False).network_address}/64"
    return str(ip)


class RateLimiter:
    """Лимит запросов на адрес за окно времени (в памяти процесса). Потокобезопасен: зависимости FastAPI
    выполняются в пуле потоков."""

    def __init__(self, limit: int, window: float = 60.0, detail: str = "Слишком много запросов, попробуйте через минуту"):
        self.limit, self.window, self.detail = limit, window, detail
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    @staticmethod
    def client_ip(request: Request) -> str:
        """Адрес посетителя. За локальным туннелем или доверенным прокси request.client — это сам прокси: берём
        последний (добавленный им) адрес из X-Forwarded-For, левые записи подделываются клиентом."""
        host = request.client.host if request.client else "unknown"
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded and (config.TRUST_PROXY or host in ("127.0.0.1", "::1")):
            host = forwarded.split(",")[-1].strip() or host
        return client_key(host)

    def allow(self, key: str) -> bool:
        """Засчитывает обращение и говорит, укладывается ли оно в лимит."""
        now = time.monotonic()
        with self._lock:
            if len(self.hits) > 5000:  # забываем давно ушедших посетителей, чтобы словарь не рос бесконечно
                for stale in [k for k, v in self.hits.items() if not v or now - v[-1] > self.window]:
                    del self.hits[stale]
            hits = self.hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def blocked(self, key: str) -> bool:
        """Исчерпан ли лимит (без засчитывания нового обращения)."""
        now = time.monotonic()
        with self._lock:
            hits = self.hits.get(key)
            return bool(hits) and sum(1 for t in hits if now - t <= self.window) >= self.limit

    def __call__(self, request: Request) -> None:
        if not self.allow(self.client_ip(request)):
            raise HTTPException(429, self.detail, headers={"Retry-After": str(int(self.window))})


ask_limiter = RateLimiter(limit=20)  # платный ИИ-эндпоинт
api_limiter = RateLimiter(limit=config.API_RATE_LIMIT)  # всё API: защита от выкачивания и перегрузки
auth_limiter = RateLimiter(limit=10, detail="Слишком много неудачных попыток, попробуйте через минуту")  # подбор ключа


# ---------- бюджет вызовов модели ----------

class LlmBudget:
    """Не даёт ИИ-эндпоинту занять все потоки сервера и сжечь квоту: одновременно работает не больше
    max_concurrency запросов к модели, за сутки — не больше daily_limit. Сверх бюджета отвечает режим правил."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self._day = date.min
        self._used = 0

    def acquire(self) -> bool:
        today = config.now_local().date()
        with self._lock:
            if today != self._day:
                self._day, self._used = today, 0
            if self._active >= config.LLM_MAX_CONCURRENCY:
                return False
            if config.LLM_DAILY_LIMIT and self._used >= config.LLM_DAILY_LIMIT:
                return False
            self._active += 1
            self._used += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    def reset(self) -> None:
        with self._lock:
            self._active, self._used, self._day = 0, 0, date.min


llm_budget = LlmBudget()


# ---------- заголовки безопасности ----------

# Скрипты только свои и Telegram (мини-приложение); встраивать страницу во фрейм можно только самому Telegram.
CSP_PAGE = "; ".join((
    "default-src 'self'",
    "script-src 'self' https://telegram.org",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https:",
    "connect-src 'self'",
    "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
))
# Swagger UI берёт скрипт и стили с CDN и запускается встроенным скриптом.
CSP_DOCS = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    "img-src 'self' data: https://fastapi.tiangolo.com",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "object-src 'none'",
))
CSP_API = "default-src 'none'; frame-ancestors 'none'"


def csp_for(path: str) -> str:
    if path in ("/docs", "/redoc") or path.startswith("/docs/"):
        return CSP_DOCS
    if path.startswith("/api/") or path == "/openapi.json":
        return CSP_API
    return CSP_PAGE


def security_headers(path: str, *, https: bool) -> dict[str, str]:
    headers = {
        "Content-Security-Policy": csp_for(path),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=(), usb=()",
    }
    if csp_for(path) is not CSP_PAGE:  # страницу показывает во фрейме Telegram, остальному фреймы не нужны
        headers["X-Frame-Options"] = "DENY"
    if https:
        headers["Strict-Transport-Security"] = "max-age=31536000"
    if path in ("/api/ask", "/api/v1/ask"):
        headers["Cache-Control"] = "no-store"
    return headers


# ---------- проверки запроса ----------

def _origin_host(value: str) -> str:
    return urlsplit(value).netloc.lower()


def cross_site_write(request: Request) -> bool:
    """Изменяющий запрос, пришедший из браузера с чужого сайта. Серверные клиенты (curl, парсеры) заголовок Origin
    не шлют и сюда не попадают; свой сайт и мини-приложение шлют Origin своего же адреса."""
    if request.method in SAFE_METHODS:
        return False
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return True
    origin = request.headers.get("origin", "")
    if not origin or origin == "null":
        return origin == "null"
    allowed = {request.headers.get("host", "").lower(), _origin_host(config.PUBLIC_URL), _origin_host(config.WEBAPP_URL)}
    return _origin_host(origin) not in allowed - {""}


def body_too_large(request: Request) -> JSONResponse | None:
    """Отказ до чтения тела: размер объявлен в Content-Length, без него тело не принимаем."""
    if request.method in SAFE_METHODS:
        return None
    declared = request.headers.get("content-length")
    if declared is None:
        if request.headers.get("transfer-encoding"):
            return error_response(411, "Нужен заголовок Content-Length")
        return None  # запрос без тела
    limit = MAX_BODY_BULK if request.url.path in BULK_PATHS else MAX_BODY
    if not declared.isdigit() or int(declared) > limit:
        return error_response(413, f"Тело запроса больше {limit // 1024} КБ")
    return None


async def guard(request: Request, call_next):
    """Единая точка защиты: размер тела, чужие POST, общий лимит API, заголовки безопасности на каждый ответ."""
    path = request.url.path
    response = body_too_large(request)
    if response is None and path.startswith("/api/"):
        if cross_site_write(request):
            response = error_response(403, "Изменяющие запросы с других сайтов запрещены")
        elif path not in HEALTH_PATHS and not api_limiter.allow(RateLimiter.client_ip(request)):
            response = error_response(429, api_limiter.detail, headers={"Retry-After": "60"})
    if response is None:
        response = await call_next(request)
    for name, value in security_headers(path, https=request.url.scheme == "https").items():
        response.headers.setdefault(name, value)
    return response
