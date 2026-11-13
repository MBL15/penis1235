"""Подписки в REST API: владелец чата определяется подписью Telegram Mini App, а не номером из тела запроса."""
import pytest

from app import config, subscriptions
from app.telegram_auth import make_init_data, validate_init_data

TOKEN = "123456:TEST-TOKEN"


@pytest.fixture(autouse=True)
def bot_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)


def auth(user_id: int, **kw):
    return {"X-Telegram-Init-Data": make_init_data(user_id, TOKEN, **kw)}


def test_init_data_is_verified_with_the_bot_token():
    assert validate_init_data(make_init_data(42, TOKEN), TOKEN) == 42
    assert validate_init_data(make_init_data(42, TOKEN), "999:OTHER") is None  # чужой токен
    tampered = make_init_data(42, TOKEN).replace("%22id%22%3A42", "%22id%22%3A43")
    assert tampered != make_init_data(42, TOKEN) and validate_init_data(tampered, TOKEN) is None  # id подменён
    assert validate_init_data(make_init_data(42, TOKEN, auth_date=1), TOKEN) is None  # устарело
    assert validate_init_data("", TOKEN) is None and validate_init_data("hash=abc", TOKEN) is None


def test_subscribe_list_and_unsubscribe(client, add, session):
    from datetime import timedelta

    from app.config import now_local

    add("Концерт «Вечер романсов»", now_local() + timedelta(days=3), city="Уфа", category="concert")
    created = client.post("/api/v1/subscriptions", json={"city": "Уфа", "category": "concert"}, headers=auth(42))
    assert created.status_code == 201 and created.json()["city"] == "Уфа" and "Уфа" in created.json()["label"]
    assert client.post("/api/v1/subscriptions", json={"city": "Уфа", "category": "concert"}, headers=auth(42)).status_code == 409
    assert [s["category"] for s in client.get("/api/v1/subscriptions", headers=auth(42)).json()] == ["concert"]
    assert client.get("/api/v1/subscriptions", headers=auth(7)).json() == []  # чужие подписки не видны
    sub_id = created.json()["id"]
    assert client.delete(f"/api/v1/subscriptions/{sub_id}", headers=auth(7)).status_code == 404  # чужую не удалить
    assert client.delete(f"/api/v1/subscriptions/{sub_id}", headers=auth(42)).status_code == 204
    assert subscriptions.list_for(session, 42) == []


def test_unsubscribe_from_everything_with_one_call(client, add):
    from datetime import timedelta

    from app.config import now_local

    add("Концерт", now_local() + timedelta(days=3), city="Уфа", category="concert")
    client.post("/api/v1/subscriptions", json={"city": "Уфа"}, headers=auth(5))
    client.post("/api/v1/subscriptions", json={"category": "concert"}, headers=auth(5))
    assert client.delete("/api/v1/subscriptions", headers=auth(5)).json() == {"removed": 2}


def test_requests_without_valid_signature_are_refused(client, monkeypatch):
    assert client.get("/api/v1/subscriptions").status_code == 401
    assert client.get("/api/v1/subscriptions", headers={"X-Telegram-Init-Data": "hash=bad"}).status_code == 401
    assert client.post("/api/v1/subscriptions", json={"chat_id": 1, "city": ""}).status_code == 401  # номер из тела не принимается
    body = client.get("/api/v1/subscriptions").json()
    assert body["error"]["code"] and "Telegram" in body["error"]["message"]
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    assert client.get("/api/v1/subscriptions", headers=auth(1)).status_code == 503  # токена нет: API честно говорит, куда идти


def test_unknown_city_and_category_are_rejected(client, add):
    from datetime import timedelta

    from app.config import now_local

    add("Концерт", now_local() + timedelta(days=3), city="Уфа", category="concert")
    assert client.post("/api/v1/subscriptions", json={"city": "Атлантида"}, headers=auth(1)).status_code == 422
    assert client.post("/api/v1/subscriptions", json={"category": "nonsense"}, headers=auth(1)).status_code == 422
