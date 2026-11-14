"""Проверка карточек перед публикацией и по расписанию (ТЗ §6.5): ссылка источника отвечает, дата в будущем, событие не дубль.

Каждая карточка получает `verified_at` и `confidence` (0–1). Новое событие из внешнего источника попадает в выдачу только если
проверка пройдена и уверенность не ниже порога, иначе оно остаётся в очереди модерации с причиной (`review_reason`).

Уверенность складывается из пяти частей:
  ссылка источника  0,35 (открывается) · 0,15 (не проверялась или сайт не отвечает «да/нет») · 0 (нет ссылки или битая)
  дата              0,20 (в будущем или событие идёт сейчас) · 0 (прошла)
  полнота           до 0,20 (площадка, адрес, цена, время начала, описание, возраст: по 1/6)
  источники         0,15 (два и больше) · 0,05 (один)
  не дубль          0,10
Блокируют публикацию: битая ссылка, прошедшая дата, дубль более старой карточки.

Эта проверка работает без ИИ. Запуск: python -m app.verify run [--pending | --all] [--no-links]"""
import argparse
import http.client
import ipaddress
import logging
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import audit, config
from .config import now_local
from .models import Event

log = logging.getLogger("afisha.verify")

AUTO = "авто: "  # причина с этим началом поставлена автоматикой: такую карточку проверка может опубликовать сама
USER_AGENT = f"bashafisha-hackathon/1.0 (+{config.PUBLIC_URL})"


# ---------- безопасная проверка ссылки ----------

@dataclass
class LinkResult:
    status: str  # ok | dead | unknown | unchecked | none
    code: int | None = None
    reason: str = ""


def public_url(url: str) -> tuple[bool, str]:
    """Ссылку можно открывать только если это http(s) и адрес публичный: иначе запись с адресом вида http://127.0.0.1/…
    заставила бы наш сервер обращаться к внутренним службам (SSRF)."""
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False, "не http(s)-ссылка"
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False, "адрес не найден"
    if not all(_is_public_ip(info[4][0]) for info in infos):
        return False, "внутренний адрес"
    return True, ""


def _is_public_ip(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


class InternalAddressError(OSError):
    """Имя хоста в момент соединения указывает на внутренний адрес: соединение не открывается."""


def pinned_connection(address: tuple[str, int], timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **_):
    """Замена socket.create_connection для проверки ссылок: имя разрешается один раз, каждый адрес проверяется, и соединение
    открывается именно с проверенным адресом.

    Проверка public_url перед запросом сама по себе не спасает: urllib разрешает имя ещё раз, и DNS с коротким временем жизни
    записи успевает ответить уже внутренним адресом (DNS rebinding). Здесь между проверкой и соединением второго разрешения нет."""
    host, port = address
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)  # gaierror уходит наружу: urllib превратит её в «адрес не найден»
    if not infos or not all(_is_public_ip(info[4][0]) for info in infos):
        raise InternalAddressError(f"внутренний адрес: {host}")
    last: OSError | None = None
    for family, kind, proto, _, sockaddr in infos:
        sock = socket.socket(family, kind, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    raise last or OSError(f"не удалось соединиться с {host}")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = pinned_connection


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Сертификат по-прежнему проверяется по имени хоста (SNI и check_hostname): к проверенному IP подключаемся только на уровне TCP."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = pinned_connection


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=getattr(self, "_context", None))


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, why = public_url(newurl)
        if not ok:
            raise urllib.error.URLError(f"перенаправление на недопустимый адрес: {why}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_link_opener() -> urllib.request.OpenerDirector:
    """Открыватель ссылок для проверки. Прокси окружения не используется (как и у сборщика источников, app/sources/base.py):
    через прокси имя разрешает уже он, и привязать соединение к проверенному адресу нельзя. AFISHA_USE_PROXY=1 включает прокси:
    тогда остаётся проверка public_url перед запросом и при каждом перенаправлении."""
    if os.getenv("AFISHA_USE_PROXY") == "1":
        return urllib.request.build_opener(_CheckedRedirects())
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _PinnedHTTPHandler(), _PinnedHTTPSHandler(), _CheckedRedirects())


def _default_open(request: urllib.request.Request, timeout: float):
    return build_link_opener().open(request, timeout=timeout)


def check_link(url: str, *, timeout: float | None = None, opener=None) -> LinkResult:
    """HEAD, а если сайт его не принимает, то GET с чтением первых байт. Временные сбои (5xx, 403/429, таймаут) не считаются
    «битой ссылкой»: сайт мог закрыться от робота, это не доказательство, что страницы нет."""
    if not url:
        return LinkResult("none", reason="нет ссылки на источник")
    ok, why = public_url(url)
    if not ok:
        return LinkResult("dead" if why == "адрес не найден" else "unknown", reason=why)
    timeout = timeout or config.VERIFY_LINK_TIMEOUT
    opener = opener or _default_open
    last = LinkResult("unknown", reason="нет ответа")
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"})
        try:
            response = opener(request, timeout)
            try:
                code = getattr(response, "status", None) or response.getcode()
                if method == "GET":
                    response.read(512)
            finally:  # иначе каждое соединение висит открытым до сборки мусора: проверка идёт по сотням ссылок
                close = getattr(response, "close", None)
                if close is not None:
                    close()
            if 200 <= code < 400:
                return LinkResult("ok", code)
            last = LinkResult("unknown", code, f"ответ {code}")
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in (404, 410):
                return LinkResult("dead", exc.code, f"страница не найдена ({exc.code})")
            last = LinkResult("unknown", exc.code, f"ответ {exc.code}")
            if method == "HEAD":
                continue  # часть сайтов не принимает HEAD (405, 403, 425…): пробуем GET, прежде чем сомневаться в ссылке
            return last
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            if isinstance(getattr(exc, "reason", None), socket.gaierror):
                return LinkResult("dead", reason="адрес не найден")
            last = LinkResult("unknown", reason=reason[:120])
            break
        except (TimeoutError, OSError) as exc:
            last = LinkResult("unknown", reason=str(exc)[:120] or "таймаут")
            break
    return last


# ---------- оценка ----------

@dataclass
class Assessment:
    confidence: float
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _is_current(event: Event, now: datetime) -> bool:
    last = max(d for d in (event.start_at, event.end_at, event.series_until) if d is not None)
    return last >= now.replace(hour=0, minute=0, second=0, microsecond=0)


def completeness(event: Event) -> float:
    parts = [
        bool(event.venue), bool(event.address), event.price_min is not None,
        (event.start_at.hour, event.start_at.minute) != (0, 0), len(event.description or "") >= 20, event.age_limit is not None,
    ]
    return sum(parts) / len(parts)


def assess(event: Event, *, now: datetime, link: LinkResult | None, duplicate_of: int | None = None) -> Assessment:
    score, blockers, notes = 0.0, [], []
    status = link.status if link else "unchecked"
    if status == "ok":
        score += 0.35
    elif status in ("unchecked", "unknown"):
        score += 0.15
        if status == "unknown" and link and link.reason:
            notes.append(f"ссылка не подтверждена: {link.reason}")
    elif status == "dead":
        blockers.append(f"ссылка не открывается: {link.reason}" if link and link.reason else "ссылка не открывается")
    else:  # none
        notes.append("нет ссылки на источник")
    if _is_current(event, now):
        score += 0.20
    else:
        blockers.append("дата уже прошла")
    score += 0.20 * completeness(event)
    score += 0.15 if len(event.sources or []) >= 2 else 0.05
    if duplicate_of is None:
        score += 0.10
    else:
        blockers.append(f"возможный дубль карточки №{duplicate_of}")
    return Assessment(round(min(score, 1.0), 2), blockers, notes)


def find_older_duplicate(session: Session, event: Event) -> int | None:
    """Более старая опубликованная карточка с тем же ключом дедупликации и временем начала: дублем считается новая."""
    if not event.dedup_key:
        return None
    return session.scalar(select(Event.id).where(
        Event.dedup_key == event.dedup_key, Event.start_at == event.start_at, Event.id < event.id,
        Event.publication == "published", Event.status != "cancelled").limit(1))


# ---------- проверка пачки ----------

@dataclass
class VerifyReport:
    checked: int = 0
    published: int = 0  # из очереди в выдачу
    queued: int = 0  # осталось в очереди или попало в неё
    demoted: int = 0  # снято с публикации (битая ссылка, дубль)
    link_ok: int = 0
    link_dead: int = 0
    link_unknown: int = 0
    skipped_past: int = 0
    reasons: dict[int, str] = field(default_factory=dict)


def _is_auto(event: Event) -> bool:
    return (event.review_reason or "").startswith(AUTO)


def verify_events(session: Session, events, *, links: bool = True, now: datetime | None = None, pause: float = 1.0,
                  demote: bool = True, actor: str = "verify", opener=None, sleep=time.sleep) -> VerifyReport:
    now = now or now_local()
    report = VerifyReport()
    seen_links: dict[str, LinkResult] = {}
    last_host_at: dict[str, float] = {}
    for event in events:
        if not _is_current(event, now):
            report.skipped_past += 1
            continue
        link = None
        if links:
            url = event.url or next((s.get("url") for s in event.sources or [] if s.get("url")), "")
            if url in seen_links:
                link = seen_links[url]
            else:
                host = urlsplit(url).hostname or ""
                wait = pause - (time.monotonic() - last_host_at.get(host, -1e9))
                if host and wait > 0:
                    sleep(wait)  # не чаще одного запроса в pause секунд к одному сайту
                link = seen_links[url] = check_link(url, opener=opener)
                last_host_at[host] = time.monotonic()
            report.link_ok += link.status == "ok"
            report.link_dead += link.status == "dead"
            report.link_unknown += link.status in ("unknown", "none")
        result = assess(event, now=now, link=link, duplicate_of=find_older_duplicate(session, event))
        before = audit.snapshot(event)
        event.verified_at, event.confidence = now, result.confidence
        report.checked += 1
        problems = result.blockers or ([f"низкая уверенность ({result.confidence:.2f})"] if result.confidence < config.VERIFY_MIN_CONFIDENCE else [])
        if event.publication == "draft":
            if _is_auto(event):  # решает автоматика; чужие (ручные) черновики проверка не публикует
                if problems:
                    event.review_reason = AUTO + "; ".join(problems)
                    report.queued += 1
                    report.reasons[event.id] = event.review_reason
                else:
                    event.publication, event.review_reason = "published", None
                    report.published += 1
            else:
                report.queued += 1
        elif problems and demote and result.blockers and "дата уже прошла" not in result.blockers:
            event.publication, event.review_reason = "draft", AUTO + "; ".join(result.blockers)
            report.demoted += 1
            report.reasons[event.id] = event.review_reason
        changes = audit.diff(before, audit.snapshot(event))
        if "publication" in changes:
            audit.record(session, actor, "publication", event.id, changes)
        # Коммит после каждой карточки: иначе транзакция записи держалась бы всю проверку вместе с запросами к сайтам и паузами
        # (минуты), и сайт с ботом в это время получали бы «database is locked» на любую запись (трассы, обращения, диалоги).
        session.commit()
    session.commit()
    return report


def pending(session: Session) -> list[Event]:
    return list(session.scalars(select(Event).where(Event.publication == "draft", Event.review_reason.like(AUTO + "%")).order_by(Event.id)))


def stale(session: Session, days: int, limit: int) -> list[Event]:
    """Опубликованные будущие события, которые не проверялись дольше days дней (или никогда): самые давние первыми."""
    horizon = now_local() - timedelta(days=days)
    stmt = select(Event).where(Event.publication == "published", or_(Event.verified_at.is_(None), Event.verified_at < horizon),
                               Event.start_at >= now_local().replace(hour=0, minute=0)).order_by(Event.verified_at.is_(None).desc(),
                                                                                                    Event.verified_at, Event.id).limit(limit)
    return list(session.scalars(stmt))


def verify_pending(session: Session, *, links: bool = True, pause: float = 1.0, opener=None) -> VerifyReport:
    return verify_events(session, pending(session), links=links, pause=pause, opener=opener)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка карточек: ссылка, дата, дубль, уверенность")
    parser.add_argument("task", choices=["run", "stats"])
    parser.add_argument("--all", action="store_true", help="проверить все будущие опубликованные события, а не только давно не проверявшиеся")
    parser.add_argument("--no-links", action="store_true", help="не ходить по ссылкам (только дата, полнота, дубли)")
    parser.add_argument("--limit", type=int, default=200, help="сколько опубликованных событий проверить за запуск")
    parser.add_argument("--pause", type=float, default=1.0, help="пауза между запросами к одному сайту, секунд")
    parser.add_argument("--keep", action="store_true", help="не снимать с публикации события с битой ссылкой, только отметить")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    from .db import SessionLocal, init_db

    init_db()
    with SessionLocal() as session:
        if args.task == "stats":
            total = session.query(Event).count()
            checked = session.query(Event).filter(Event.verified_at.is_not(None)).count()
            queue = session.query(Event).filter(Event.publication == "draft").count()
            print(f"Событий: {total}; проверено: {checked}; в очереди модерации: {queue}; ждут автоматической проверки: {len(pending(session))}")
            return 0
        events = pending(session) + (list(session.scalars(select(Event).where(Event.publication == "published", Event.start_at >= now_local().replace(hour=0, minute=0)))) if args.all
                                     else stale(session, config.VERIFY_STALE_DAYS, args.limit))
        report = verify_events(session, events, links=not args.no_links, pause=args.pause, demote=not args.keep)
    print(f"Проверено {report.checked}; опубликовано из очереди {report.published}; в очереди {report.queued}; снято с публикации {report.demoted}; "
          f"ссылки: открываются {report.link_ok}, битые {report.link_dead}, не подтверждены {report.link_unknown}; прошедших пропущено {report.skipped_past}")
    for event_id, reason in list(report.reasons.items())[:20]:
        print(f"  №{event_id}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
