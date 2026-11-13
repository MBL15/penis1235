"""Наблюдаемость (ТЗ §6.1, §6.3, §10): идентификатор запроса, структурные логи без персональных данных, метрики."""
import json
import logging
import re

import pytest

from app import config
from app.observability import JsonFormatter, metrics, new_request_id, request_id_var, route_label


@pytest.fixture(autouse=True)
def fresh_metrics():
    metrics.reset()
    yield


def test_every_response_carries_a_request_id_and_a_valid_client_id_is_kept(client):
    generated = client.get("/api/v1/health").headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", generated)
    assert client.get("/api/v1/health", headers={"X-Request-ID": "trace-2026-11-13-abc"}).headers["x-request-id"] == "trace-2026-11-13-abc"
    for bad in ("short", "x" * 65, "id with spaces", "a;b/c12345", "<script>alert(1)</script>"):
        assert client.get("/api/v1/health", headers={"X-Request-ID": bad}).headers["x-request-id"] != bad
    assert new_request_id("bad!") != "bad!"


def test_error_body_has_the_same_request_id_as_the_header(client):
    response = client.get("/api/v1/events/999999", headers={"X-Request-ID": "trace-error-0001"})
    assert response.headers["x-request-id"] == response.json()["error"]["request_id"] == "trace-error-0001"


def test_request_log_has_no_personal_data(client, caplog):
    with caplog.at_level(logging.INFO, logger="afisha.request"):
        client.get("/api/v1/events", params={"q": "секретный запрос"}, headers={"X-Forwarded-For": "203.0.113.7"})
    (record,) = [r for r in caplog.records if r.name == "afisha.request"]
    line = json.loads(JsonFormatter().format(record))
    assert line["method"] == "GET" and line["path"] == "/api/v1/events" and line["status"] == 200
    assert line["duration_ms"] >= 0 and len(line["request_id"]) == 32
    text = json.dumps(line, ensure_ascii=False)
    for private in ("секретный", "203.0.113.7", "testclient", "127.0.0.1"):  # строка запроса и адреса в лог не попадают
        assert private not in text


def test_health_checks_are_not_logged_at_info_level(client, caplog):
    with caplog.at_level(logging.INFO, logger="afisha.request"):
        client.get("/health")
        client.get("/api/v1/health")
    assert not [r for r in caplog.records if r.name == "afisha.request"]


def test_json_formatter_outputs_one_valid_json_line_per_record():
    token = request_id_var.set("abc-123-request")
    try:
        record = logging.LogRecord("afisha.x", logging.WARNING, __file__, 1, "Сбой %s", ("источника",), None)
        record.fields = {"route": "/api/v1/events"}
        data = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert data["msg"] == "Сбой источника" and data["level"] == "warning" and data["request_id"] == "abc-123-request"
    assert data["route"] == "/api/v1/events" and "\n" not in JsonFormatter().format(record)


def test_route_labels_do_not_grow_with_the_number_of_events():
    assert route_label("/api/v1/events/12345", matched=True) == "/api/v1/events/{id}"
    assert route_label("/api/v1/events/12345/status", matched=True) == "/api/v1/events/{id}/status"
    assert route_label("/wp-admin/setup.php", matched=False) == "unmatched"
    assert route_label("/static/app.js", matched=True) == "/static/*"


def test_metrics_count_requests_errors_and_latency(client, monkeypatch):
    client.get("/api/v1/events")
    client.get("/api/v1/events")
    client.get("/api/v1/events/4242")  # 404
    client.get("/nowhere")  # адрес, которого нет

    def boom(*args, **kwargs):
        raise RuntimeError("сбой базы")

    monkeypatch.setattr("app.api_public.search", boom)
    failed = client.get("/api/v1/events")
    assert failed.status_code == 500 and failed.json()["error"]["code"] == "internal_error"
    assert "сбой базы" not in failed.text  # внутренности наружу не отдаём

    text = client.get("/metrics").text
    assert 'afisha_http_requests_total{method="GET",route="/api/v1/events",status="200"} 2' in text
    assert 'afisha_http_requests_total{method="GET",route="/api/v1/events/{id}",status="404"} 1' in text
    assert 'afisha_http_requests_total{method="GET",route="unmatched",status="404"} 1' in text
    assert 'afisha_http_errors_total{route="/api/v1/events"} 1' in text
    assert 'afisha_http_request_duration_seconds_bucket{route="/api/v1/events",le="+Inf"} 3' in text
    assert 'afisha_http_request_duration_seconds_count{route="/api/v1/events"} 3' in text
    assert "afisha_events_current 0" in text and "afisha_uptime_seconds" in text


def test_ask_counter_shows_which_mode_answered(client):
    client.post("/api/v1/ask", json={"message": "бесплатно в Уфе"})
    assert 'afisha_ask_total{mode="rules",provider="none"} 1' in client.get("/metrics").text


def test_metrics_can_be_protected_with_a_token(client, monkeypatch):
    monkeypatch.setattr(config, "METRICS_TOKEN", "secret-metrics-token")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer secret-metrics-token"}).status_code == 200


def test_liveness_and_readiness(client):
    assert client.get("/health").json()["status"] == "ok"
    ready = client.get("/api/v1/health").json()
    assert ready["status"] == "ok" and ready["version"] == config.API_VERSION
    assert client.get("/api/health").status_code == 200  # прежний адрес: на него смотрит healthcheck контейнера
