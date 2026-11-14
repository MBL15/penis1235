"""Вход на сайт через бота (без виджета telegram.org и /setdomain): ссылка t.me/<бот>?start=login_<код> → кнопка в боте → вход."""
from datetime import timedelta

import pytest

from app import accounts, config
from app.models import LoginRequest
from app.telegram_auth import TelegramUser


@pytest.fixture(autouse=True)
def bot_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:TEST")
    monkeypatch.setattr(config, "TELEGRAM_BOT_USERNAME", "BashSZbot")


def start(client):
    res = client.post("/api/v1/auth/telegram/bot", json={})
    assert res.status_code == 200
    data = res.json()
    assert data["url"] == f"https://t.me/BashSZbot?start=login_{data['code']}" and data["expires_in"] == 300
    return data["code"]


def test_full_login_through_the_bot(client, session):
    code = start(client)
    assert client.get(f"/api/v1/auth/telegram/bot/{code}").status_code == 202  # ещё не подтвердили в боте
    assert client.get("/api/v1/me").json()["authenticated"] is False
    assert accounts.confirm_bot_login(session, code, TelegramUser(777, "Айгуль Х."))
    done = client.get(f"/api/v1/auth/telegram/bot/{code}")
    assert done.status_code == 200 and done.json()["user"]["display_name"] == "Айгуль Х."
    me = client.get("/api/v1/me").json()  # кука входа поставлена
    assert me["authenticated"] is True
    from app.models import User

    assert session.get(User, me["user"]["id"]).telegram_id == 777  # аккаунт привязан к тому, кто нажал кнопку в боте
    assert client.get(f"/api/v1/auth/telegram/bot/{code}").status_code in (403, 410)  # второй раз тем же кодом не войти
    assert session.get(LoginRequest, accounts.hash_token(code)).telegram_id == 777  # в базе хеш кода, не сам код


def test_only_the_browser_that_started_can_finish(client, session):
    code = start(client)
    accounts.confirm_bot_login(session, code, TelegramUser(5, "Иван"))
    client.cookies.clear()  # другой браузер: нет секрета из куки
    assert client.get(f"/api/v1/auth/telegram/bot/{code}").status_code == 403


def test_expired_or_cancelled_link_cannot_be_confirmed(client, session):
    code = start(client)
    row = session.get(LoginRequest, accounts.hash_token(code))
    row.expires_at = row.expires_at - timedelta(minutes=10)
    session.commit()
    assert accounts.pending_bot_login(session, code) is None
    assert not accounts.confirm_bot_login(session, code, TelegramUser(5, "Иван"))
    assert client.get(f"/api/v1/auth/telegram/bot/{code}").status_code == 410
    other = start(client)
    accounts.cancel_bot_login(session, other)  # «Это не я» в боте
    assert not accounts.confirm_bot_login(session, other, TelegramUser(5, "Иван"))


def test_bad_codes_are_rejected(client, session):
    assert accounts.pending_bot_login(session, "../../etc") is None
    assert client.get("/api/v1/auth/telegram/bot/short").status_code == 422


def test_login_is_off_without_a_bot_token(client, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    assert client.post("/api/v1/auth/telegram/bot", json={}).status_code == 503
