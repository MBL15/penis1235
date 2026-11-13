"""Открытый контракт API (ТЗ §6.3): версия в адресе, единый формат ошибок, кеширование, OpenAPI."""
from datetime import datetime
from pathlib import Path

import pytest

from app import config
from app.security import ask_limiter

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


# ---------- версия ----------

def test_v1_and_the_old_paths_return_the_same_data(client, add):
    add("Концерт", datetime(2026, 12, 20, 19, 0), category="concert", price_min=500)
    new = client.get("/api/v1/events", params={"include_past": True}).json()
    old = client.get("/api/events", params={"include_past": True}).json()
    assert new == old and new["total"] == 1
    assert client.get("/api/v1/meta").json() == client.get("/api/meta").json()


def test_openapi_describes_only_the_versioned_api(client):
    spec = client.get("/openapi.json").json()
    assert spec["openapi"].startswith("3.1") and spec["info"]["version"] == config.API_VERSION
    paths = set(spec["paths"])
    assert {"/api/v1/events", "/api/v1/events/{event_id}", "/api/v1/meta", "/api/v1/ask", "/api/v1/health", "/health"} <= paths
    assert not [p for p in paths if p.startswith("/api/") and not p.startswith("/api/v1/")]
    assert "ErrorOut" in spec["components"]["schemas"]
    assert "/api/v1" in spec["info"]["description"] and "X-Request-ID" in spec["info"]["description"]
    example = spec["components"]["schemas"]["EventIn"]["examples"][0]
    assert example["title"] and example["source"]


def test_site_and_widget_use_the_versioned_api():
    for name in ("app.js", "widget.js"):
        script = (STATIC / name).read_text(encoding="utf-8")
        assert "/api/v1/" in script and "'/api/events" not in script and "`/api/events" not in script, name


# ---------- единый формат ошибок ----------

def assert_error(response, status, code):
    body = response.json()
    assert response.status_code == status and set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message", "details", "request_id"} and error["code"] == code
    assert any("а" <= ch.lower() <= "я" for ch in error["message"]), error["message"]  # сообщение по-русски
    assert error["request_id"] == response.headers["x-request-id"]
    return error


def test_every_kind_of_error_has_the_same_shape(client, admin_key):
    assert_error(client.get("/api/v1/events/999999"), 404, "not_found")
    assert_error(client.get("/api/v1/no-such-method"), 404, "not_found")
    assert_error(client.delete("/api/v1/events"), 405, "method_not_allowed")
    assert_error(client.get("/api/v1/events", params={"when": "someday"}), 422, "validation_error")
    assert_error(client.post("/api/v1/events", json={}), 401, "unauthorized")
    assert_error(client.post("/api/v1/ask", json={"message": "x"}, headers={"Origin": "https://evil.example"}), 403, "forbidden")
    assert_error(client.post("/api/v1/ask", content=b"x" * (1024 * 1024 + 1), headers={"Content-Type": "application/json"}),
                 413, "payload_too_large")


def test_validation_errors_name_the_field_in_plain_russian(client, admin_key):
    error = assert_error(client.get("/api/v1/events", params={"limit": "abc", "age": 200}), 422, "validation_error")
    assert error["message"] == "Некорректные параметры запроса"
    by_field = {d["field"]: d["message"] for d in error["details"]}
    assert by_field["limit"] == "нужно целое число" and by_field["age"] == "не больше 99"

    error = assert_error(client.post("/api/v1/events", json={"start_at": "вчера", "source": ""}, headers=admin_key),
                         422, "validation_error")
    assert error["message"] == "Некорректное тело запроса"
    by_field = {d["field"]: d["message"] for d in error["details"]}
    assert by_field["title"] == "обязательное поле" and "ГГГГ-ММ-ДД" in by_field["start_at"] and "source" in by_field

    bad_url = assert_error(client.post("/api/v1/events", json={"title": "Концерт", "start_at": "2026-12-01T19:00", "source": "x",
                                                               "url": "javascript:alert(1)"}, headers=admin_key), 422, "validation_error")
    assert any(d["field"] == "url" and "http" in d["message"] for d in bad_url["details"])  # сообщения наших проверок уже по-русски
    assert "Input should" not in str(bad_url)  # английских текстов pydantic в ответе нет


def test_rate_limit_error_tells_when_to_retry(client, monkeypatch):
    monkeypatch.setattr(ask_limiter, "limit", 1)
    client.post("/api/v1/ask", json={"message": "бесплатно"})
    response = client.post("/api/v1/ask", json={"message": "бесплатно"})
    assert_error(response, 429, "rate_limited")
    assert response.headers["retry-after"] == "60"


# ---------- кеширование ----------

def test_event_card_has_etag_and_cache_control_and_answers_304(client, add):
    event, _ = add("Концерт", datetime(2026, 12, 20, 19, 0), category="concert", price_min=500)
    url = f"/api/v1/events/{event.id}"
    first = client.get(url)
    etag = first.headers["etag"]
    assert first.status_code == 200 and etag.startswith('W/"') and first.headers["cache-control"] == "public, max-age=60"

    again = client.get(url, headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == etag
    assert client.get(url, headers={"If-None-Match": f'"other", {etag}'}).status_code == 304
    assert client.get(url, headers={"If-None-Match": "*"}).status_code == 304
    assert client.get(url, headers={"If-None-Match": 'W/"stale"'}).status_code == 200


def test_changed_event_gets_a_new_etag(client, add, session):
    event, _ = add("Концерт", datetime(2026, 12, 20, 19, 0), price_min=500)
    url = f"/api/v1/events/{event.id}"
    etag = client.get(url).headers["etag"]
    event.price_min = 700
    session.commit()
    changed = client.get(url, headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag and changed.json()["price_min"] == 700


def test_directories_and_lists_are_cacheable(client):
    meta = client.get("/api/v1/meta")
    assert meta.headers["cache-control"] == "public, max-age=300" and meta.headers["etag"]
    listing = client.get("/api/v1/events")
    assert listing.headers["cache-control"] == "public, max-age=30"
    assert client.get("/api/v1/events", headers={"If-None-Match": listing.headers["etag"]}).status_code == 304


def test_other_sites_can_read_the_validators(client):
    response = client.get("/api/v1/meta", headers={"Origin": "https://ministry.example"})
    assert "etag" in response.headers["access-control-expose-headers"].lower()
    preflight = client.options("/api/v1/events", headers={
        "Origin": "https://ministry.example", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "if-none-match"})
    assert preflight.status_code == 200
