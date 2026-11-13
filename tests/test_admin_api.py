"""Методы модератора (ТЗ §3, §6.3): список с черновиками, правка, публикация, импорт файла, журнал, личные ключи."""
import json
from datetime import timedelta

import pytest

from app import config
from app.config import now_local
from app.models import AuditLog

START = (now_local() + timedelta(days=6)).replace(hour=19, minute=0, second=0, microsecond=0)
ANNA = "anna-key-" + "a" * 32
BORIS = "boris-key-" + "b" * 32


def rec(title="Квартет «Осень»", source="manual", **kw):
    return {"title": title, "start_at": START.isoformat(), "source": source, "city": "Уфа", **kw}


@pytest.fixture
def moderators(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_API_KEY", "")
    monkeypatch.setattr(config, "ADMIN_USERS", f"anna:{ANNA}, boris:{BORIS}")
    return {"anna": {"X-API-Key": ANNA}, "boris": {"X-API-Key": BORIS}}


def seed(client, admin_key, **kw):
    return client.post("/api/v1/events", json=rec(**kw), headers=admin_key).json()["event"]


# ---------- доступ ----------

ENDPOINTS = [
    ("get", "/api/v1/admin/session"), ("get", "/api/v1/admin/events"), ("get", "/api/v1/admin/events/1"),
    ("patch", "/api/v1/admin/events/1"), ("post", "/api/v1/admin/events/1/publication"), ("post", "/api/v1/admin/import"),
    ("get", "/api/v1/admin/audit"), ("get", "/api/v1/admin/feedback"), ("patch", "/api/v1/admin/feedback/1"),
]


@pytest.mark.parametrize("method, url", ENDPOINTS)
def test_every_admin_method_needs_a_key(client, admin_key, method, url):
    assert getattr(client, method)(url).status_code == 401
    assert getattr(client, method)(url, headers={"X-API-Key": "wrong"}).status_code == 401


def test_session_names_the_moderator_and_the_old_single_key_still_works(client, admin_key, moderators, monkeypatch):
    assert client.get("/api/v1/admin/session", headers=moderators["anna"]).json()["actor"] == "anna"
    assert client.get("/api/v1/admin/session", headers=moderators["boris"]).json()["actor"] == "boris"
    monkeypatch.setattr(config, "ADMIN_API_KEY", "shared-key-" + "s" * 32)
    assert client.get("/api/v1/admin/session", headers={"X-API-Key": "shared-key-" + "s" * 32}).json()["actor"] == "admin"
    assert client.get("/api/v1/admin/session", headers={"X-API-Key": "x" * 40}).status_code == 401


def test_short_or_badly_named_keys_are_ignored(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_API_KEY", "")
    monkeypatch.setattr(config, "ADMIN_USERS", "ivan:short, bad name!:" + "k" * 40)
    response = client.get("/api/v1/admin/session", headers={"X-API-Key": "k" * 40})
    assert response.status_code == 503 and "короче" in response.json()["error"]["message"]
    monkeypatch.setattr(config, "ADMIN_USERS", "")
    assert "не задан" in client.get("/api/v1/admin/session").json()["error"]["message"]


# ---------- события для модератора ----------

def test_admin_list_shows_drafts_cancelled_and_past_events(client, admin_key):
    published = seed(client, admin_key, title="Опубликованное событие")
    client.post("/api/v1/events/bulk?publish=false", json=[rec("Черновик события")], headers=admin_key)
    cancelled = seed(client, admin_key, title="Отменённое событие", status="cancelled")
    old = client.post("/api/v1/events", json={**rec("Давнее событие"), "start_at": "2026-10-01T19:00:00"}, headers=admin_key).json()["event"]

    everything = client.get("/api/v1/admin/events", headers=admin_key).json()
    assert {e["title"] for e in everything["items"]} == {"Опубликованное событие", "Черновик события", "Отменённое событие", "Давнее событие"}
    public = client.get("/api/v1/events", params={"include_past": True, "status": "all"}).json()
    assert {e["title"] for e in public["items"]} == {"Опубликованное событие", "Отменённое событие", "Давнее событие"}  # черновика нет

    queue = client.get("/api/v1/admin/events", params={"publication": "draft"}, headers=admin_key).json()
    assert [e["title"] for e in queue["items"]] == ["Черновик события"] and queue["items"][0]["publication"] == "draft"
    assert client.get("/api/v1/admin/events", params={"publication": "published", "q": "отменённое"}, headers=admin_key).json()["total"] == 1
    assert client.get("/api/v1/admin/events", params={"status": "cancelled"}, headers=admin_key).json()["total"] == 1
    assert client.get("/api/v1/admin/events", params={"status": "boom"}, headers=admin_key).status_code == 422
    assert client.get("/api/v1/admin/events", params={"publication": "x"}, headers=admin_key).status_code == 422
    assert published["id"] and cancelled["id"] and old["id"]


def test_a_draft_is_visible_to_the_moderator_only(client, admin_key):
    draft = client.post("/api/v1/events/bulk?publish=false", json=[rec()], headers=admin_key).json()["events"][0]
    assert client.get(f"/api/v1/admin/events/{draft['id']}", headers=admin_key).json()["publication"] == "draft"
    assert client.get(f"/api/v1/events/{draft['id']}").status_code == 404
    assert client.get("/api/v1/admin/events/999999", headers=admin_key).status_code == 404


# ---------- правка ----------

def test_edit_changes_only_the_given_fields_and_is_journaled_under_the_moderators_name(client, moderators, session):
    created = seed(client, moderators["boris"], price_min=300, price_max=900)
    response = client.patch(f"/api/v1/admin/events/{created['id']}", headers={**moderators["anna"], "X-Request-ID": "trace-edit-0001"},
                            json={"title": "Квартет «Весна»", "city": "в Стерлитамаке", "price_max": 1200, "description": "Новое описание"})
    event = response.json()
    assert response.status_code == 200 and event["title"] == "Квартет «Весна»" and event["city"] == "Стерлитамак"
    assert (event["price_min"], event["price_max"]) == (300, 1200) and event["description"] == "Новое описание"
    entry = session.query(AuditLog).filter_by(action="edit").one()
    assert entry.actor == "anna" and entry.event_id == created["id"] and entry.request_id == "trace-edit-0001"
    assert entry.changes["title"] == ["Квартет «Осень»", "Квартет «Весна»"] and entry.changes["price_max"] == [900, 1200]
    assert "price_min" not in entry.changes
    created_entry = session.query(AuditLog).filter_by(action="create").one()
    assert created_entry.actor == "boris"  # кто создал и кто правил видно по журналу
    # поиск понимает новое название
    assert client.get("/api/v1/events", params={"q": "весна"}).json()["total"] == 1
    assert client.get("/api/v1/events", params={"q": "осень"}).json()["total"] == 0


def test_edit_can_clear_optional_fields(client, admin_key):
    created = seed(client, admin_key, price_min=300, price_max=900, end_at=(START + timedelta(hours=2)).isoformat())
    event = client.patch(f"/api/v1/admin/events/{created['id']}", json={"price_min": None, "price_max": None, "end_at": None},
                         headers=admin_key).json()
    assert event["price_min"] is None and event["price_text"] == "Цена не указана" and event["end_at"] is None


@pytest.mark.parametrize("body, text", [
    ({}, "Не передано ни одного поля"),
    ({"title": None}, "нельзя очистить"),
    ({"city": " "}, "нельзя очистить"),
    ({"category": "теаатр"}, "Неизвестная категория"),
    ({"end_at": "2020-01-01T10:00:00"}, "окончания раньше"),
    ({"price_min": 900, "price_max": 100}, "Цена «до» меньше"),
])
def test_edit_rejects_inconsistent_values_with_a_plain_message(client, admin_key, body, text, session):
    created = seed(client, admin_key, price_min=300, price_max=900)
    response = client.patch(f"/api/v1/admin/events/{created['id']}", json=body, headers=admin_key)
    assert response.status_code == 422 and text in response.json()["error"]["message"]
    session.expire_all()
    assert client.get(f"/api/v1/events/{created['id']}").json()["price_min"] == 300  # ничего не изменилось
    assert not session.query(AuditLog).filter_by(action="edit").count()


def test_edit_validates_input_like_the_import_does(client, admin_key):
    created = seed(client, admin_key)
    url = f"/api/v1/admin/events/{created['id']}"
    assert client.patch(url, json={"url": "javascript:alert(1)"}, headers=admin_key).status_code == 422
    assert client.patch(url, json={"price_min": -1}, headers=admin_key).status_code == 422
    assert client.patch(url, json={"unknown_field": 1}, headers=admin_key).status_code == 422
    assert client.patch(url, json={"age_limit": 150}, headers=admin_key).status_code == 422
    assert client.patch("/api/v1/admin/events/999999", json={"title": "Ок"}, headers=admin_key).status_code == 404
    assert client.patch(url, json={"age_limit": "12+", "kids_friendly": True, "url": "https://tickets.example/9"},
                        headers=admin_key).json()["age_limit"] == 12


# ---------- публикация и очередь модерации ----------

def test_publishing_moves_a_draft_into_the_public_listing(client, moderators, session):
    draft = client.post("/api/v1/events/bulk?publish=false", json=[rec("Событие из очереди")], headers=moderators["boris"]).json()["events"][0]
    assert client.get("/api/v1/events", params={"q": "очереди"}).json()["total"] == 0
    published = client.post(f"/api/v1/admin/events/{draft['id']}/publication", json={"publication": "published"}, headers=moderators["anna"])
    assert published.status_code == 200 and published.json()["publication"] == "published"
    assert client.get("/api/v1/events", params={"q": "очереди"}).json()["total"] == 1
    assert client.get(f"/api/v1/events/{draft['id']}").status_code == 200
    entry = session.query(AuditLog).filter_by(action="publication").one()
    assert entry.actor == "anna" and entry.changes["publication"] == ["draft", "published"]

    again = client.post(f"/api/v1/admin/events/{draft['id']}/publication", json={"publication": "published"}, headers=moderators["anna"])
    assert again.status_code == 200 and session.query(AuditLog).filter_by(action="publication").count() == 1  # без изменений журнал не пухнет
    client.post(f"/api/v1/admin/events/{draft['id']}/publication", json={"publication": "draft"}, headers=moderators["anna"])
    assert client.get(f"/api/v1/events/{draft['id']}").status_code == 404  # снято с публикации
    assert client.post(f"/api/v1/admin/events/{draft['id']}/publication", json={"publication": "x"}, headers=moderators["anna"]).status_code == 422


def test_cancelling_from_the_admin_hides_the_event_but_keeps_its_link(client, admin_key):
    created = seed(client, admin_key)
    client.patch(f"/api/v1/events/{created['id']}/status", json={"status": "cancelled"}, headers=admin_key)
    assert client.get("/api/v1/events", params={"q": "осень"}).json()["total"] == 0
    assert client.get(f"/event/{created['id']}").status_code == 200


# ---------- импорт файла ----------

def test_json_import_reports_created_updated_and_rejected(client, moderators, session):
    content = json.dumps([rec("Первое событие"), {"title": "Без даты", "source": "x"}, rec("Второе событие", price_min=0)], ensure_ascii=False)
    body = {"filename": "events.json", "content": content}
    first = client.post("/api/v1/admin/import", json=body, headers=moderators["anna"]).json()
    assert (first["created"], first["updated"], first["unchanged"], first["total"]) == (2, 0, 0, 3)
    assert first["rejected"] == [{"row": 2, "title": "Без даты", "reasons": ["start_at: обязательное поле"]}]
    assert {e["title"] for e in first["events"]} == {"Первое событие", "Второе событие"}
    again = client.post("/api/v1/admin/import", json=body, headers=moderators["anna"]).json()
    assert (again["created"], again["unchanged"]) == (0, 2)  # повторная загрузка того же файла дублей не создаёт
    assert session.query(AuditLog).filter(AuditLog.actor == "anna:import:events.json").count() == 2


def test_csv_import_and_drafts_option(client, admin_key):
    csv_text = f"title,start_at,source,city,price_min\nКвартет «Зима»,{START.isoformat()},lib.example,Уфа,0\nБез даты,,lib.example,Уфа,5\n"
    report = client.post("/api/v1/admin/import", json={"filename": "events.CSV", "content": csv_text, "publish": False}, headers=admin_key).json()
    assert report["created"] == 1 and report["rejected"][0]["row"] == 3  # в CSV вторая строка данных — третья строка файла
    assert report["events"][0]["publication"] == "draft"
    assert client.get("/api/v1/events", params={"q": "зима"}).json()["total"] == 0
    assert client.get("/api/v1/admin/events", params={"publication": "draft"}, headers=admin_key).json()["total"] == 1


@pytest.mark.parametrize("filename, content, text", [
    ("events.json", "{oops", "корректным JSON"),
    ("events.json", json.dumps({"x": 1}), "список событий"),
    ("events.xml", "<x/>", "Неизвестный формат"),
    ("events", "[]", "Неизвестный формат"),
])
def test_import_explains_a_broken_file(client, admin_key, filename, content, text):
    response = client.post("/api/v1/admin/import", json={"filename": filename, "content": content}, headers=admin_key)
    assert response.status_code == 422 and text in response.json()["error"]["message"]


def test_import_size_is_limited(client, admin_key):
    content = json.dumps([{"title": "x"}] * 1001)
    response = client.post("/api/v1/admin/import", json={"filename": "big.json", "content": content}, headers=admin_key)
    assert response.status_code == 413 and "1001" in response.json()["error"]["message"]


# ---------- журнал ----------

def test_audit_endpoint_filters_by_event_and_actor(client, moderators):
    first = seed(client, moderators["boris"], title="Первое событие")
    second = seed(client, moderators["anna"], title="Второе событие")
    client.patch(f"/api/v1/admin/events/{first['id']}", json={"venue": "Филармония"}, headers=moderators["anna"])
    everything = client.get("/api/v1/admin/audit", headers=moderators["anna"]).json()
    assert [e["action"] for e in everything] == ["edit", "create", "create"]  # новые записи первыми
    assert set(everything[0]) == {"id", "at", "actor", "action", "event_id", "changes", "request_id"}
    assert [e["actor"] for e in client.get("/api/v1/admin/audit", params={"event_id": first["id"]}, headers=moderators["anna"]).json()] == ["anna", "boris"]
    assert [e["event_id"] for e in client.get("/api/v1/admin/audit", params={"actor": "anna"}, headers=moderators["anna"]).json()] == [first["id"], second["id"]]
    assert len(client.get("/api/v1/admin/audit", params={"limit": 1}, headers=moderators["anna"]).json()) == 1
    assert client.get("/api/v1/admin/audit", params={"limit": 0}, headers=moderators["anna"]).status_code == 422


def test_admin_responses_are_never_cached_and_pages_are_not_indexed(client):
    page = client.get("/admin")
    assert page.status_code == 200 and "noindex" in page.headers["x-robots-tag"]
