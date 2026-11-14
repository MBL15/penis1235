"""Защита: заголовки, чужие сайты, лимиты, проверка входных данных, бюджет вызовов модели."""
import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import agent, config, security
from app.main import app
from app.schemas import EventIn
from app.security import RateLimiter, api_limiter, auth_limiter, client_key, llm_budget
from app.sources import culture_ru

from .conftest import NOW
from .test_api import payload
from .test_gemini import FakeGemini, final

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


# ---------- заголовки ----------

def test_pages_get_a_strict_content_security_policy(client):
    headers = client.get("/").headers
    csp = headers["content-security-policy"]
    assert "script-src 'self' https://telegram.org" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "object-src 'none'" in csp and "frame-ancestors 'self' https://web.telegram.org" in csp
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "x-frame-options" not in headers  # страницу открывает во фрейме Telegram: правило задаёт frame-ancestors
    assert "strict-transport-security" not in headers  # по http этот заголовок не имеет смысла


def test_api_and_docs_get_their_own_policies(client):
    api = client.get("/api/events").headers
    assert api["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'" and api["x-frame-options"] == "DENY"
    docs = client.get("/docs").headers
    assert "https://cdn.jsdelivr.net" in docs["content-security-policy"] and docs["x-frame-options"] == "DENY"
    assert client.post("/api/ask", json={"message": "что посоветуешь?"}).headers["cache-control"] == "no-store"


def test_https_responses_carry_hsts(session):
    with TestClient(app, base_url="https://testserver") as secure:
        assert secure.get("/api/health").headers["strict-transport-security"] == "max-age=31536000"


def test_pages_have_no_inline_scripts_or_handlers():
    """CSP запрещает встроенные скрипты: если они вернутся в разметку, страница молча сломается."""
    for name in ("index.html", "widget-demo.html", "admin.html", "feedback.html"):
        html = (STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), name
        assert not re.search(r"\son[a-z]+\s*=", html), name
    assert client_served_scripts_exist()


def client_served_scripts_exist() -> bool:
    return all((STATIC / name).is_file() for name in ("boot.js", "widget-demo.js", "app.js", "widget.js", "admin.js", "feedback.js"))


# ---------- чужие сайты ----------

def test_other_sites_may_read_but_not_write(client):
    read = client.get("/api/events", headers={"Origin": "https://ministry.example"})
    assert read.status_code == 200 and read.headers["access-control-allow-origin"] == "*"

    preflight = client.options("/api/ask", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    assert preflight.status_code == 400

    question = {"message": "что посоветуешь?"}
    assert client.post("/api/ask", json=question, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/ask", json=question, headers={"Origin": "null"}).status_code == 403
    assert client.post("/api/ask", json=question, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_own_site_and_server_clients_may_write(client, monkeypatch):
    question = {"message": "что посоветуешь?"}
    assert client.post("/api/ask", json=question).status_code == 200  # curl и парсеры Origin не шлют
    assert client.post("/api/ask", json=question, headers={"Origin": "http://testserver"}).status_code == 200
    monkeypatch.setattr(config, "WEBAPP_URL", "https://afisha.example")  # мини-приложение на своём домене
    assert client.post("/api/ask", json=question, headers={"Origin": "https://afisha.example"}).status_code == 200


def test_untrusted_host_header_is_rejected_when_hosts_are_listed():
    from fastapi import FastAPI
    from fastapi.middleware.trustedhost import TrustedHostMiddleware

    guarded = FastAPI()
    guarded.add_middleware(TrustedHostMiddleware, allowed_hosts=["afisha.example"])
    assert TestClient(guarded, base_url="http://evil.example").get("/").status_code == 400


# ---------- размер и форма запроса ----------

def test_oversized_bodies_are_refused_before_reading(client):
    big = "я" * (security.MAX_BODY + 10)
    response = client.post("/api/ask", content=big.encode(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert "content-security-policy" in response.headers  # заголовки стоят и на отказах


def test_search_query_is_bounded(client, add):
    add("Джазовый вечер", datetime(2026, 11, 8, 19, 0))
    assert client.get("/api/events", params={"q": "слово " * 400}).status_code == 422
    many_words = " ".join(f"w{i}" for i in range(40))  # в пределах длины, но слов больше лимита: лишние отбрасываются
    assert client.get("/api/events", params={"q": many_words, "include_past": True}).status_code == 200
    assert client.get("/api/events", params={"city": "у" * 65}).status_code == 422
    assert client.get("/api/events", params={"status": ["cancelled"] * 9}).status_code == 422
    assert client.get("/api/events/99999999999999999999").status_code == 422


def test_history_longer_than_a_model_reply_is_accepted_and_trimmed(client):
    long_reply = "Очень подробный ответ. " * 300  # ~6900 символов: столько бывает в развёрнутом ответе модели
    response = client.post("/api/ask", json={"message": "бесплатно на выходных",
                                             "history": [{"role": "user", "content": "привет"}, {"role": "assistant", "content": long_reply}]})
    assert response.status_code == 200
    turns = [{"role": "user", "content": "в" * 5000}] * 30
    trimmed = agent._history_messages(turns)
    assert len(trimmed) == agent.MAX_HISTORY_TURNS and all(len(t["content"]) == agent.MAX_TURN_CHARS for t in trimmed)


# ---------- ключ приёма данных ----------

def test_non_ascii_key_is_a_plain_401(client, admin_key):
    response = client.post("/api/events", json=payload(), headers={"X-API-Key": bytes([233, 233, 233])})
    assert response.status_code == 401


def test_short_admin_key_disables_ingest(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_API_KEY", "short")
    response = client.post("/api/events", json=payload(), headers={"X-API-Key": "short"})
    assert response.status_code == 503 and "короче" in response.json()["error"]["message"]


def test_key_guessing_is_throttled(client, admin_key):
    for _ in range(auth_limiter.limit):
        assert client.post("/api/events", json=payload(), headers={"X-API-Key": "guess"}).status_code == 401
    locked = client.post("/api/events", json=payload(), headers=admin_key)  # даже верный ключ минуту не принимается
    assert locked.status_code == 429 and locked.headers["retry-after"] == "60"
    auth_limiter.hits.clear()
    assert client.post("/api/events", json=payload(), headers=admin_key).status_code == 200


# ---------- входные данные события ----------

@pytest.mark.parametrize("field", ["url", "image_url", "source_url"])
@pytest.mark.parametrize("value", ["javascript:alert(1)", "data:text/html,<script>1</script>", "//evil.example", "https://ok.example/a b"])
def test_only_http_links_are_stored(client, admin_key, field, value):
    assert client.post("/api/events", json=payload(**{field: value}), headers=admin_key).status_code == 422


def test_event_fields_are_bounded(client, admin_key):
    for bad in ({"price_min": 10**30}, {"price_max": 10**12}, {"description": "о" * 20_001}, {"venue": "з" * 201},
                {"age_limit": 200}, {"age_limit": "1" * 17}, {"title": "   "}, {"city": "г" * 65}):
        assert client.post("/api/events", json=payload(**bad), headers=admin_key).status_code == 422, bad


def test_small_source_mistakes_are_repaired_not_rejected():
    event = EventIn(title="Концерт", start_at="2026-11-20T19:00:00", end_at="2026-11-20T18:00:00",
                    price_min=900, price_max=300, source="s", url=" https://ok.example/x ")
    assert event.end_at is None and (event.price_min, event.price_max) == (300, 900) and event.url == "https://ok.example/x"


# ---------- лимиты ----------

def test_whole_api_is_rate_limited_per_address(client, monkeypatch):
    monkeypatch.setattr(api_limiter, "limit", 3)
    assert [client.get("/api/meta").status_code for _ in range(4)] == [200, 200, 200, 429]
    assert client.get("/api/health").status_code == 200  # проверка живости под лимит не попадает
    assert client.get("/").status_code == 200  # страницы тоже


def test_ipv6_clients_are_limited_by_their_64_subnet():
    assert client_key("2001:db8:1:2:aaaa::1") == client_key("2001:db8:1:2:bbbb::9") == "2001:db8:1:2::/64"
    assert client_key("2001:db8:1:3::1") != client_key("2001:db8:1:2::1")
    assert client_key("::ffff:203.0.113.7") == client_key("203.0.113.7") == "203.0.113.7"
    assert client_key("не адрес" * 30) == ("не адрес" * 30)[:64]


def test_limiter_is_safe_under_threads():
    from concurrent.futures import ThreadPoolExecutor

    limiter = RateLimiter(limit=50)
    with ThreadPoolExecutor(16) as pool:
        allowed = list(pool.map(lambda _: limiter.allow("one"), range(400)))
    assert sum(allowed) == 50


# ---------- бюджет вызовов модели ----------

@pytest.fixture
def llm(monkeypatch, add):
    monkeypatch.setattr(config, "AGENT_MODE", "llm")
    llm_budget.reset()
    add("Бесплатная выставка", datetime(2026, 11, 8, 12, 0), category="exhibition", price_min=0)
    yield
    llm_budget.reset()


def test_model_is_not_called_beyond_the_concurrency_budget(session, llm, monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_CONCURRENCY", 1)
    assert llm_budget.acquire()  # один запрос к модели уже идёт
    gemini = FakeGemini(final("ответ модели", []))
    result = agent.ask(session, "бесплатно на выходных", client=gemini, now=NOW)
    assert result.mode == "rules" and not gemini.calls
    llm_budget.release()
    assert agent.ask(session, "бесплатно на выходных", client=gemini, now=NOW).mode == "llm"


def test_daily_budget_falls_back_to_rules_and_slot_is_released(session, llm, monkeypatch):
    monkeypatch.setattr(config, "LLM_DAILY_LIMIT", 2)
    monkeypatch.setattr(config, "LLM_MAX_CONCURRENCY", 1)
    gemini = FakeGemini(final("раз", []), RuntimeError("сеть упала"), final("не дойдёт", []))
    modes = [agent.ask(session, "бесплатно на выходных", client=gemini, now=NOW).mode for _ in range(3)]
    assert modes == ["llm", "rules", "rules"] and len(gemini.calls) == 2  # третий запрос модель уже не трогает


def test_answer_has_a_time_budget(session, llm, monkeypatch):
    monkeypatch.setattr(config, "LLM_TIME_BUDGET", -1)  # время вышло ещё до первого шага
    gemini = FakeGemini(final("не дойдёт", []))
    assert agent.ask(session, "бесплатно на выходных", client=gemini, now=NOW).mode == "rules" and not gemini.calls


def test_long_question_is_cut_before_reaching_the_model(session, llm):
    gemini = FakeGemini(final("ответ", []))
    agent.ask(session, "я" * 5000, client=gemini, now=NOW)
    sent = gemini.calls[0]["contents"][-1].parts[0].text
    assert len(sent) == agent.MAX_MESSAGE_CHARS


# ---------- загрузчик источника ----------

class _Response:
    def __init__(self, url, body):
        self.url, self.body = url, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.body[:size] if size >= 0 else self.body


def _opener(response):
    class Opener:
        def open(self, request, timeout=None):
            return response
    return lambda *handlers: Opener()


def test_source_fetch_refuses_foreign_redirects_and_huge_pages(monkeypatch):
    target = culture_ru.LISTING
    monkeypatch.setattr(culture_ru.urllib.request, "build_opener", _opener(_Response("https://evil.example/x", b"ok")))
    with pytest.raises(culture_ru.SourceError, match="посторонний адрес"):
        culture_ru.http_get(target, retries=1)
    monkeypatch.setattr(culture_ru.urllib.request, "build_opener", _opener(_Response("http://www.culture.ru/afisha", b"ok")))
    with pytest.raises(culture_ru.SourceError, match="посторонний адрес"):  # понижение до http
        culture_ru.http_get(target, retries=1)
    huge = b"x" * (culture_ru.MAX_RESPONSE_BYTES + 5)
    monkeypatch.setattr(culture_ru.urllib.request, "build_opener", _opener(_Response(target, huge)))
    with pytest.raises(culture_ru.SourceError, match="больше"):
        culture_ru.http_get(target, retries=1)
    monkeypatch.setattr(culture_ru.urllib.request, "build_opener", _opener(_Response(target, "страница".encode())))
    assert culture_ru.http_get(target, retries=1) == "страница"


def test_forwarded_for_is_trusted_only_from_internal_peers(monkeypatch):
    """Б-2: порт сервиса открыт наружу, TRUST_PROXY=1 — подставной X-Forwarded-For с публичного адреса не меняет ключ лимита."""
    from types import SimpleNamespace

    from app.security import RateLimiter, internal_peer

    monkeypatch.setattr(config, "TRUST_PROXY", True)
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [])

    def request(peer):
        return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"x-forwarded-for": "1.1.1.1, 5.5.5.5"})

    assert RateLimiter.client_ip(request("172.18.0.1")) == "5.5.5.5"  # nginx в сети Docker
    assert RateLimiter.client_ip(request("8.8.4.4")) == "8.8.4.4"  # напрямую из интернета: заголовок не принят
    assert internal_peer("10.0.0.5") and internal_peer("::1") and not internal_peer("8.8.8.8")
    import ipaddress

    monkeypatch.setattr(config, "TRUSTED_PROXIES", [ipaddress.ip_network("172.17.0.1/32")])
    assert RateLimiter.client_ip(request("172.18.0.1")) == "172.18.0.1"  # не из списка
    assert RateLimiter.client_ip(request("172.17.0.1")) == "5.5.5.5"
