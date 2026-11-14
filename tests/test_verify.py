"""Проверка карточек перед публикацией: ссылка, дата, дубль, уверенность, очередь модерации."""
import socket
import urllib.error
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import config, moderation, verify
from app.config import now_local
from app.models import Event
from app.pipeline import process_record
from app.search import SearchFilters, search
from app.verify import AUTO, LinkResult, assess, check_link, public_url, verify_events, verify_pending

NOW = now_local().replace(hour=9, minute=0, second=0, microsecond=0)
START = NOW + timedelta(days=3, hours=10)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Имена хостов в тестах «разрешаются» в публичный адрес; реальный DNS не нужен."""
    def fake(host, port, type=0, **kw):
        import ipaddress
        try:
            ipaddress.ip_address(host)  # адрес-литерал разрешается сам в себя, как и в настоящем getaddrinfo
            return [(2, 1, 6, "", (host, port))]
        except ValueError:
            pass
        if host in ("localhost", "internal.example"):
            return [(2, 1, 6, "", ("127.0.0.1", port))]
        if host == "gone.example":
            raise socket.gaierror("no such host")
        return [(2, 1, 6, "", ("93.184.216.34", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


def opener_returning(*outcomes):
    """Подмена urlopen: список ответов по порядку (код или исключение)."""
    calls = []
    queue = list(outcomes)

    def opener(request, timeout):
        calls.append((request.get_method(), request.full_url))
        item = queue.pop(0) if queue else 200
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int) and item >= 400:
            raise urllib.error.HTTPError(request.full_url, item, "x", {}, None)
        return SimpleNamespace(status=item, getcode=lambda: item, read=lambda n=0: b"")

    opener.calls = calls
    return opener


def http_error(url, code):
    return urllib.error.HTTPError(url, code, "x", {}, None)


# ---------- ссылки ----------

def test_internal_addresses_are_never_requested():
    for url in ("http://localhost/admin", "http://internal.example/x", "http://127.0.0.1:8000/", "http://169.254.169.254/latest", "ftp://x.example/", "javascript:alert(1)"):
        assert public_url(url)[0] is False, url
    assert public_url("https://example.org/e/1")[0] is True
    opener = opener_returning()
    result = check_link("http://localhost/admin", opener=opener)
    assert result.status == "unknown" and not opener.calls  # сеть не тронута


def test_link_statuses():
    assert check_link("https://example.org/e", opener=opener_returning(200)).status == "ok"
    assert check_link("https://example.org/e", opener=opener_returning(301)).status == "ok"
    dead = check_link("https://example.org/e", opener=opener_returning(http_error("u", 404)))
    assert dead.status == "dead" and dead.code == 404
    assert check_link("https://gone.example/e", opener=opener_returning()).status == "dead"  # хоста больше нет
    assert check_link("https://example.org/e", opener=opener_returning(http_error("u", 503), http_error("u", 503))).status == "unknown"  # временный сбой (HEAD и GET)
    assert check_link("https://example.org/e", opener=opener_returning(http_error("u", 429), http_error("u", 429))).status == "unknown"  # просят остановиться
    assert check_link("https://example.org/e", opener=opener_returning(TimeoutError("slow"))).status == "unknown"
    assert check_link("", opener=opener_returning()).status == "none"


def test_head_not_allowed_falls_back_to_get():
    opener = opener_returning(http_error("u", 405), 200)
    assert check_link("https://example.org/e", opener=opener).status == "ok"
    assert [m for m, _ in opener.calls] == ["HEAD", "GET"]


# ---------- оценка ----------

def make(session, add, title="Концерт «Вечер романсов»", url=None, venue="Филармония", **kw):
    # у разных событий разные ссылки и площадки: иначе дедупликация склеит их в одно
    url = url or f"https://example.org/e/{sum(map(ord, title))}"
    event, _ = add(title, START, city="Уфа", venue=venue, address="ул. Ленина, 5", price_min=500, age_limit=12,
                   description="Камерный оркестр исполнит романсы русских композиторов.", url=url, **kw)
    return event


def test_confidence_adds_up_from_link_date_completeness_sources_and_uniqueness(session, add):
    event = make(session, add)
    best = assess(event, now=NOW, link=LinkResult("ok", 200))
    assert best.confidence == pytest.approx(0.35 + 0.20 + 0.20 + 0.05 + 0.10) and not best.blockers
    unchecked = assess(event, now=NOW, link=None)
    assert unchecked.confidence == pytest.approx(best.confidence - 0.20)  # ссылка не проверялась: нейтрально, а не провал
    bare, _ = add("Событие без деталей", START, city="Уфа", venue="", price_min=None)
    assert assess(bare, now=NOW, link=LinkResult("none")).confidence < config.VERIFY_MIN_CONFIDENCE  # нет ссылки и данных


def test_blockers_dead_link_past_date_duplicate(session, add):
    event = make(session, add)
    assert "ссылка не открывается" in assess(event, now=NOW, link=LinkResult("dead", 404, "страница не найдена (404)")).blockers[0]
    old = make(session, add, title="Старое событие")
    old.start_at = NOW - timedelta(days=5)
    assert "дата уже прошла" in assess(old, now=NOW, link=LinkResult("ok", 200)).blockers
    assert any("дубль" in b for b in assess(event, now=NOW, link=LinkResult("ok", 200), duplicate_of=1).blockers)


# ---------- очередь и публикация ----------

def queued(session, title="Новое событие", **kw):
    """Событие из внешнего источника: попадает в очередь и ждёт автоматической проверки."""
    record = {"title": title, "start_at": START.isoformat(), "city": "Уфа", "venue": "Филармония", "address": "ул. Ленина, 5",
              "price_min": 300, "age_limit": 6, "description": "Подробное описание события для карточки.",
              "url": "https://example.org/e/9", "source": "kassir.ru", **kw}
    result = process_record(session, record, actor="sync:test", publish=False, auto_queue=True)
    return result.event


def test_new_event_from_a_source_waits_in_the_queue_and_is_hidden(session):
    event = queued(session)
    assert event.publication == "draft" and event.review_reason == AUTO + "ждёт проверки"
    assert search(session, SearchFilters(), NOW)[1] == 0  # в выдаче нет


def test_good_card_is_published_after_verification(session):
    event = queued(session)
    report = verify_pending(session, links=True, pause=0, opener=opener_returning(200))
    session.refresh(event)
    assert report.published == 1 and event.publication == "published" and event.review_reason is None
    assert event.verified_at is not None and event.confidence >= config.VERIFY_MIN_CONFIDENCE
    assert search(session, SearchFilters(), NOW)[1] == 1


def test_card_with_dead_link_stays_in_the_queue_with_the_reason(session):
    event = queued(session)
    report = verify_pending(session, links=True, pause=0, opener=opener_returning(http_error("u", 404)))
    session.refresh(event)
    assert report.queued == 1 and report.link_dead == 1 and event.publication == "draft"
    assert event.review_reason.startswith(AUTO) and "ссылка не открывается" in event.review_reason
    assert search(session, SearchFilters(), NOW)[1] == 0


def test_low_confidence_card_stays_in_the_queue(session):
    event = queued(session, url="", venue="", address="", price_min=None, description="", age_limit=None)
    verify_pending(session, links=False, pause=0)
    session.refresh(event)
    assert event.publication == "draft" and "низкая уверенность" in event.review_reason


def test_manual_draft_is_never_published_by_verification(session, add):
    event = make(session, add)
    moderation.set_publication(session, event, "draft", "ivanov")
    assert event.review_reason == "модератор: снято с публикации"
    verify_events(session, [event], links=True, pause=0, opener=opener_returning(200))
    assert event.publication == "draft" and event.verified_at is not None  # проверена, но решение за модератором
    moderation.set_publication(session, event, "published", "ivanov")
    assert event.review_reason is None


def test_published_card_with_dead_link_is_taken_down_and_logged(session, add):
    from sqlalchemy import select

    from app.models import AuditLog

    event = make(session, add)
    report = verify_events(session, [event], links=True, pause=0, opener=opener_returning(http_error("u", 410)))
    assert report.demoted == 1 and event.publication == "draft" and "ссылка не открывается" in event.review_reason
    entry = session.scalars(select(AuditLog).where(AuditLog.event_id == event.id, AuditLog.action == "publication")).first()
    assert entry is not None and entry.actor == "verify"
    kept = make(session, add, title="Другое событие", venue="Зал три")
    assert verify_events(session, [kept], links=True, pause=0, demote=False, opener=opener_returning(http_error("u", 410))).demoted == 0


def test_past_events_are_not_checked_and_same_link_is_requested_once(session, add):
    past = make(session, add, title="Прошедшее", venue="Театр один")
    past.start_at = NOW - timedelta(days=4)
    shared = "https://example.org/shared"
    a, b = make(session, add, title="Первое", url=shared, venue="Театр два"), make(session, add, title="Второе", url=shared, venue="Театр три")
    opener = opener_returning(200)
    report = verify_events(session, [past, a, b], links=True, pause=0, opener=opener)
    assert report.skipped_past == 1 and report.checked == 2 and len(opener.calls) == 1  # одинаковая ссылка: один запрос


def test_pause_between_requests_to_one_site(session, add):
    one, two = make(session, add, title="Один", venue="Зал один"), make(session, add, title="Два", venue="Зал два")
    sleeps = []
    verify_events(session, [one, two], links=True, pause=1.0, opener=opener_returning(), sleep=sleeps.append)
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 1.0  # второй запрос к тому же хосту ждёт


def test_sync_puts_new_events_in_the_queue_and_verification_publishes_the_good_ones(session):
    from app.sync import sync_records

    good = {"title": "Концерт «Вечер»", "start_at": START.isoformat(), "city": "Уфа", "venue": "Филармония", "address": "ул. Ленина, 5",
            "price_min": 300, "age_limit": 6, "description": "Подробное описание события для карточки.",
            "url": "https://example.org/good", "source": "kassir.ru"}
    bad = {**good, "title": "Событие с битой ссылкой", "url": "https://example.org/bad", "venue": "Другой зал"}
    stats = sync_records(session, [good, bad], actor="sync:test", queue=True)
    assert stats["created"] == 2 and search(session, SearchFilters(), NOW)[1] == 0  # до проверки в выдаче пусто

    def opener(request, timeout):
        if request.full_url.endswith("/bad"):
            raise urllib.error.HTTPError(request.full_url, 404, "x", {}, None)
        return SimpleNamespace(status=200, getcode=lambda: 200, read=lambda n=0: b"")

    report = verify_pending(session, links=True, pause=0, opener=opener)
    assert (report.published, report.queued, report.link_dead) == (1, 1, 1)
    assert [e.title for e in search(session, SearchFilters(), NOW)[0]] == ["Концерт «Вечер»"]
    waiting = session.query(Event).filter(Event.publication == "draft").one()
    assert waiting.title == "Событие с битой ссылкой" and "ссылка не открывается" in waiting.review_reason


def test_verification_does_not_hold_the_write_lock_during_link_checks(session, add, monkeypatch):
    """Проверка ссылок идёт минутами: транзакция записи не должна быть открыта, пока ждём ответа сайта (иначе сайт и бот
    получают «database is locked»). К моменту следующего запроса изменения прошлой карточки уже зафиксированы."""
    from datetime import datetime

    from app import verify
    from app.verify import LinkResult

    first, _ = add("Концерт один", datetime(2030, 1, 10, 19, 0), url="https://example.org/1")
    second, _ = add("Концерт два", datetime(2030, 1, 11, 19, 0), url="https://example.org/2")
    open_tx = []

    def check(url, **kw):
        open_tx.append(session.in_transaction() and bool(session.dirty or session.new))
        return LinkResult("ok", 200)

    monkeypatch.setattr(verify, "check_link", check)
    verify.verify_events(session, [first, second], links=True, pause=0, sleep=lambda s: None)
    assert open_tx == [False, False]
