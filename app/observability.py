"""Наблюдаемость (ТЗ §6.1, §6.3, §10): идентификатор запроса, структурные логи без персональных данных, метрики.

* У каждого запроса есть идентификатор (X-Request-ID): его можно передать самому или получить в ответе, он попадает в логи
  и в тело ошибки, поэтому жалобу пользователя можно сопоставить со строкой лога.
* Логи в формате JSON, по строке на запрос. В них нет адресов посетителей, строки запроса и тел запросов: только метод,
  маршрут, статус и время. Access-лог uvicorn с адресами отключён (setup_logging).
* Метрики в формате Prometheus на /metrics: число запросов по маршрутам и статусам, ошибки 5xx, задержка (гистограмма).
"""
import contextvars
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from . import config

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
log = logging.getLogger("afisha.request")


def new_request_id(incoming: str | None = None) -> str:
    """Принимаем идентификатор клиента, только если он безопасен для лога (буквы, цифры, ._-), иначе выдаём свой."""
    return incoming if incoming and _VALID_ID.fullmatch(incoming) else uuid.uuid4().hex


# ---------- логи ----------

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        data.update(getattr(record, "fields", None) or {})
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


_OWN_HANDLER = "_afisha_handler"


def setup_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Один обработчик на корневой логгер (повторный вызов заменяет его). Формат: LOG_FORMAT=json|text."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _OWN_HANDLER, False):
            root.removeHandler(handler)
    handler = logging.StreamHandler()
    setattr(handler, _OWN_HANDLER, True)
    handler.addFilter(_RequestIdFilter())
    if (fmt or config.LOG_FORMAT).lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel((level or config.LOG_LEVEL).upper())
    logging.getLogger("uvicorn.access").disabled = True  # он пишет адреса посетителей; свою строку запроса ведёт observe()


# ---------- метрики ----------

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
HELP = {
    "afisha_http_requests_total": "Число HTTP-запросов по методу, маршруту и статусу",
    "afisha_http_errors_total": "Число ответов 5xx по маршруту",
    "afisha_ask_total": "Число вопросов ИИ-помощнику по режиму ответа",
    "afisha_feedback_total": "Число обращений через форму обратной связи по результату",
}


def _escape(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _labels(pairs) -> str:
    if not pairs:
        return ""
    return "{" + ",".join(f'{key}="{_escape(value)}"' for key, value in pairs) + "}"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.started = time.time()
            self._counters: dict[tuple[str, tuple], float] = {}
            self._hist: dict[str, dict] = {}

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe_request(self, method: str, route: str, status: int, seconds: float) -> None:
        self.inc("afisha_http_requests_total", method=method, route=route, status=str(status))
        if status >= 500:
            self.inc("afisha_http_errors_total", route=route)
        with self._lock:
            hist = self._hist.setdefault(route, {"buckets": [0] * len(BUCKETS), "sum": 0.0, "count": 0})
            hist["sum"] += seconds
            hist["count"] += 1
            for i, bound in enumerate(BUCKETS):
                if seconds <= bound:
                    hist["buckets"][i] += 1

    def render(self, gauges: dict[str, float] | None = None) -> str:
        lines: list[str] = []
        with self._lock:
            names = sorted({name for name, _ in self._counters})
            for name in names:
                lines += [f"# HELP {name} {HELP.get(name, name)}", f"# TYPE {name} counter"]
                for (n, labels), value in sorted(self._counters.items()):
                    if n == name:
                        lines.append(f"{name}{_labels(labels)} {value:g}")
            if self._hist:
                name = "afisha_http_request_duration_seconds"
                lines += [f"# HELP {name} Время обработки запроса, секунды", f"# TYPE {name} histogram"]
                for route, hist in sorted(self._hist.items()):
                    for bound, count in zip(BUCKETS, hist["buckets"]):
                        lines.append(f"{name}_bucket{_labels([('route', route), ('le', f'{bound:g}')])} {count}")
                    lines.append(f"{name}_bucket{_labels([('route', route), ('le', '+Inf')])} {hist['count']}")
                    lines.append(f"{name}_sum{_labels([('route', route)])} {hist['sum']:.6f}")
                    lines.append(f"{name}_count{_labels([('route', route)])} {hist['count']}")
            uptime = time.time() - self.started
        for name, value in {"afisha_uptime_seconds": uptime, **(gauges or {})}.items():
            lines += [f"# TYPE {name} gauge", f"{name} {value:g}"]
        return "\n".join(lines) + "\n"


metrics = Metrics()
_ID_SEGMENT = re.compile(r"/\d+(?=/|$)")


def route_label(path: str, *, matched: bool) -> str:
    """Метка маршрута для метрик: числа в адресе заменяются на {id}, чтобы число меток не росло с числом событий.
    Адреса, которых нет в приложении (сканеры), сводятся в одну метку."""
    if path.startswith("/static/"):
        return "/static/*"
    return _ID_SEGMENT.sub("/{id}", path) if matched else "unmatched"


QUIET_ROUTES = ("/health", "/api/health", "/api/v1/health", "/metrics")  # вызываются мониторингом каждые секунды: в лог не пишем


async def observe(request, call_next):
    """Внешний слой: идентификатор запроса, одна строка лога и метрики на каждый запрос, в том числе отклонённый защитой."""
    from .errors import error_response  # импорт здесь: errors сам использует request_id_var

    request_id = new_request_id(request.headers.get("x-request-id"))
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        try:
            response = await call_next(request)
        except Exception:  # сюда доходит всё, что не обработали обработчики ошибок
            log.exception("Необработанная ошибка при обработке запроса")
            response = error_response(500, "Внутренняя ошибка сервиса. Попробуйте позже", "internal_error")
        elapsed = time.perf_counter() - started
        response.headers["X-Request-ID"] = request_id
        path = request.url.path
        route = route_label(path, matched=request.scope.get("route") is not None)
        metrics.observe_request(request.method, route, response.status_code, elapsed)
        (log.debug if path in QUIET_ROUTES else log.info)(
            "request",
            extra={"fields": {"request_id": request_id, "method": request.method, "path": path[:200], "route": route,
                              "status": response.status_code, "duration_ms": round(elapsed * 1000, 1)}},
        )
        return response
    finally:
        request_id_var.reset(token)
