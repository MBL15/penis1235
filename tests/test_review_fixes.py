"""Регрессии по итогам код-ревью: каждая проверка воспроизводит найденную ошибку."""
from datetime import datetime, time, timedelta

from app import agent_actions
from app.config import now_local
from app.models import Subscription

SOON = (now_local() + timedelta(days=3)).replace(hour=19, minute=0, second=0, microsecond=0)


# ---------- ввод, на котором сервер отвечал 500 ----------

def test_event_page_with_non_ascii_digit_is_404_not_500(client):
    """«²».isdigit() истинно, а int("²") падает: страница события отвечала 500."""
    assert client.get("/event/²").status_code == 404
    assert client.get("/event/١٢").status_code == 404


def test_feedback_token_with_non_ascii_digit_is_rejected(client):
    from app.feedback import check_token

    assert check_token("².abc")  # причина отказа, а не исключение
    response = client.post("/api/v1/feedback", json={"message": "Проверка формы обратной связи", "token": "².abc"})
    assert response.status_code == 422


def test_admin_user_search_by_odd_digits(client, admin_key):
    assert client.get("/api/v1/admin/users", params={"q": "²"}, headers=admin_key).status_code == 200
    assert client.get("/api/v1/admin/users", params={"q": "9" * 40}, headers=admin_key).status_code == 200


def test_ask_rejects_out_of_range_context_ids(client, add):
    add("Концерт", SOON, city="Уфа", category="concert")
    response = client.post("/api/v1/ask", json={"message": "а ещё", "context_event_ids": [10**30]})
    assert response.status_code == 422


def test_patch_cannot_clear_required_fields(client, admin_key, add):
    event, _ = add("Спектакль", SOON, city="Уфа")
    for field in ("start_at", "kids_friendly"):
        response = client.patch(f"/api/v1/admin/events/{event.id}", json={field: None}, headers=admin_key)
        assert response.status_code == 422, (field, response.text)


def test_agent_tools_report_out_of_range_ids_to_the_model(session):
    """Номер события от модели вне INTEGER: раньше OverflowError срывал весь ответ модели в запасной режим."""
    from app.agent_tools import ToolContext, filters_from_tool_input, run_tool
    from app.tracing import TraceBuilder

    ctx = ToolContext(session=session, now=now_local(), trace=TraceBuilder(query="q"))
    assert "error" in run_tool(ctx, "get_event", {"id": 10**30})
    assert "error" in run_tool(ctx, "find_similar", {"event_id": -5})
    assert filters_from_tool_input({"exclude_ids": [10**30, 5, 0]}).exclude_ids == [5]


def test_jsonld_price_nan_does_not_break_parsing():
    from app.sources.jsonld import prices_from_offers

    assert prices_from_offers({"price": "NaN"}) == (None, None, False)
    assert prices_from_offers([{"price": "Infinity"}, {"price": "500"}])[:2] == (500, 500)


# ---------- функциональные ошибки ----------

def test_admin_feedback_filter_in_progress(client, admin_key):
    """Фильтр «в работе» есть в интерфейсе модератора, а API отвечало на него 422."""
    assert client.get("/api/v1/admin/feedback", params={"status": "in_progress"}, headers=admin_key).status_code == 200


def test_move_subscriptions_when_two_collapse_into_one(session):
    """Подписки «Салават · театр» и «Уфа · театр» при переносе в Уфу совпадают: раньше коммит падал на уникальном ключе."""
    chat = 777
    session.add_all([Subscription(chat_id=chat, city="Салават", category="theatre"),
                     Subscription(chat_id=chat, city="Уфа", category="theatre"),
                     Subscription(chat_id=chat, city="Салават", category="concert")])
    session.commit()
    action = agent_actions.propose(session, "move_subscription", {"chat_id": chat, "city": "Уфа"}, agent_actions.summary_move("Уфа"))
    ok, _ = agent_actions.confirm(session, action.id, chat_id=chat)
    assert ok
    left = sorted((s.city, s.category) for s in session.query(Subscription).filter_by(chat_id=chat))
    assert left == [("Уфа", "concert"), ("Уфа", "theatre")]


def test_plan_budget_is_not_glued_to_previous_number():
    """«с 12 до 20 2000 рублей»: бюджет 2000, а не 202000."""
    from app.planner import parse_budget_text, request_from_text

    assert parse_budget_text("с 12 до 20 2000 рублей")[0] == 2000
    assert parse_budget_text("2 000 руб на двоих") == (2000, 2)
    assert parse_budget_text("до 1500 ₽")[0] == 1500
    assert request_from_text("суббота с 12 до 20 2000 рублей", now_local().date()).budget == 2000


def test_cinema_links_encode_city():
    from datetime import date

    from app.cinema_page import _link

    href = _link(date(2026, 11, 15), "Город & #1", date(2026, 11, 14))
    assert "#" not in href and "&amp;city=" in href and "%26" in href


def test_check_link_closes_response():
    from app.verify import check_link

    closed = []

    class Response:
        status = 200

        def getcode(self):
            return 200

        def read(self, n=-1):
            return b""

        def close(self):
            closed.append(True)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    import app.verify as verify

    original = verify.public_url
    verify.public_url = lambda url: (True, "")
    try:
        assert check_link("https://example.org/", opener=lambda request, timeout: Response()).status == "ok"
    finally:
        verify.public_url = original
    assert closed


def test_fetcher_refuses_redirect_to_foreign_host():
    import urllib.error
    import urllib.request

    import pytest

    from app.sources.base import Fetcher, SourceError

    fetcher = Fetcher(("example.org",), delay=0, retries=1, sleep=lambda s: None)
    handler = next(h for h in fetcher.opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler))
    request = urllib.request.Request("https://example.org/a")
    with pytest.raises((SourceError, urllib.error.URLError)):
        handler.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1:8080/admin")
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://www.example.org/b") is not None


# ---------- то, что в первом проходе было оставлено ----------

def _addrinfo(ip: str, port: int = 80):
    import socket

    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


class _FakeSocket:
    """Сокет без сети: запоминает, куда пытались подключиться."""

    connected: list = []

    def __init__(self, *args):
        pass

    def settimeout(self, value):
        pass

    def connect(self, address):
        _FakeSocket.connected.append(address)
        raise OSError("сети в тесте нет")

    def close(self):
        pass


def test_link_check_is_not_fooled_by_dns_rebinding(monkeypatch):
    """Первое разрешение имени (проверка public_url) — публичный адрес, второе (соединение) — внутренний. Раньше urllib
    разрешал имя заново и шёл на 127.0.0.1; теперь соединение открывается только с проверенным адресом."""
    from app import verify

    answers = [_addrinfo("93.184.216.34"), _addrinfo("127.0.0.1")]
    monkeypatch.setattr(verify.socket, "getaddrinfo", lambda *a, **k: answers.pop(0))
    _FakeSocket.connected = []
    monkeypatch.setattr(verify.socket, "socket", _FakeSocket)
    result = verify.check_link("http://rebind.test/event")
    assert result.status == "unknown" and "внутренний" in result.reason
    assert _FakeSocket.connected == []  # к внутреннему адресу соединения не было


def test_link_check_connects_to_the_checked_address(monkeypatch):
    from app import verify

    monkeypatch.setattr(verify.socket, "getaddrinfo", lambda *a, **k: _addrinfo("93.184.216.34"))
    _FakeSocket.connected = []
    monkeypatch.setattr(verify.socket, "socket", _FakeSocket)
    assert verify.check_link("http://example.test/event").status == "unknown"
    assert _FakeSocket.connected == [("93.184.216.34", 80)]


def test_link_opener_uses_pinned_handlers_without_env_proxy(monkeypatch):
    import urllib.request

    from app import verify

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("AFISHA_USE_PROXY", raising=False)
    kinds = {type(h) for h in verify.build_link_opener().handlers}
    assert verify._PinnedHTTPHandler in kinds and verify._PinnedHTTPSHandler in kinds
    assert urllib.request.HTTPSHandler not in kinds and urllib.request.HTTPHandler not in kinds
    proxies = [h for h in verify.build_link_opener().handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert all(not h.proxies for h in proxies)


def test_content_length_with_non_ascii_digit_is_rejected():
    """Сервер перед приложением такой заголовок отклоняет, но и сама защита не должна падать на int("²")."""
    from starlette.requests import Request

    from app.security import body_too_large

    request = Request({"type": "http", "method": "POST", "path": "/api/v1/feedback", "query_string": b"", "root_path": "",
                       "scheme": "http", "server": ("test", 80), "headers": [(b"content-length", "²".encode("latin-1"))]})
    response = body_too_large(request)
    assert response is not None and response.status_code == 413


def test_default_plan_day_is_consistent_with_the_word_saturday():
    from app.planner import default_day, request_from_text
    from app.rules import parse_query

    saturday = datetime(2026, 11, 14, 10, 0)
    assert default_day(saturday) == saturday.date()  # как «суббота» в субботу
    assert parse_query("суббота", saturday.date()).filters.date_from == saturday.date()
    assert default_day(saturday.replace(hour=19)) == saturday.date() + timedelta(days=7)  # вечером план уже не собрать
    assert default_day(datetime(2026, 11, 13, 23, 0)) == saturday.date()  # пятница
    assert default_day(datetime(2026, 11, 15, 9, 0)) == datetime(2026, 11, 21).date()  # воскресенье
    assert request_from_text("", saturday.date(), now=saturday).day == saturday.date()


def test_plan_for_today_skips_events_that_already_started(session, add):
    from app.planner import PlanRequest, plan_weekend

    day = (now_local() + timedelta(days=2)).date()
    now = datetime.combine(day, time(12, 0))

    def put(title, hour, minute=0):
        start = datetime.combine(day, time(hour, minute))
        add(title, start, end_at=start + timedelta(minutes=90), price_min=100, price_max=100, city="Уфа", venue=f"Зал {title}",
            url=f"https://example.org/{abs(hash(title)) % 10**6}")

    put("Уже идёт", 11, 30)  # кончится в 13:00: поиск его отдаёт, но к началу не успеть
    put("Днём", 14)
    put("Вечером", 17)
    result = plan_weekend(session, PlanRequest(day=day, events=3), now)
    assert [i.event.title for i in result.items] == ["Днём", "Вечером"]
