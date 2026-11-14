"""Аккаунты и роли (docs/08): вход через Telegram, сессии, история посещений, сохранённые поиски, обращения организатора,
роли, ключи Министерства, миграция 0005."""
import hashlib
import time
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, inspect, select, text

from app import accounts, config, db, feedback
from app.accounts import SESSION_COOKIE
from app.config import now_local
from app.models import ApiKey, Base, Feedback, SavedSearch, Subscription, User, UserSession, VisitHistory
from app.security import api_limiter, auth_limiter, login_limiter, partner_limiter
from app.telegram_auth import make_init_data, make_login_widget_data, parse_init_data, validate_login_widget

TOKEN = "123456:TEST-bot-token"
ADMIN = {"X-API-Key": "test-key-" + "k" * 32}


@pytest.fixture(autouse=True)
def _tg(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    for limiter in (partner_limiter, login_limiter, auth_limiter, api_limiter):
        limiter.hits.clear()
    accounts._pending.clear()
    accounts._flushed.clear()


@pytest.fixture
def admin(admin_key):
    return admin_key


def login(client, telegram_id=1001, **kw):
    """Входит через мини-приложение; кука остаётся в клиенте."""
    response = client.post("/api/v1/auth/telegram", json={"init_data": make_init_data(telegram_id, TOKEN, **kw.pop("init", {})), **kw})
    assert response.status_code == 200, response.text
    return response.json()


def future_event(add, title="Концерт «Тест»", **kw):
    event, _ = add(title, now_local() + timedelta(days=2), category="concert", **kw)
    return event


def make_role(client, admin, telegram_id, role):
    response = client.post("/api/v1/admin/users", json={"telegram_id": telegram_id, "role": role}, headers=admin)
    assert response.status_code == 200, response.text
    return response.json()


# ---------- подпись Telegram ----------

def test_login_widget_signature_is_checked_with_the_bot_token():
    data = make_login_widget_data(77, TOKEN)
    assert validate_login_widget(data, TOKEN).id == 77
    assert validate_login_widget(data, "999:OTHER") is None  # чужой токен
    assert validate_login_widget({**data, "id": "78"}, TOKEN) is None  # номер подменён
    assert validate_login_widget({k: v for k, v in data.items() if k != "hash"}, TOKEN) is None
    assert validate_login_widget(make_login_widget_data(77, TOKEN, auth_date=int(time.time()) - 7200), TOKEN) is None  # устарела
    assert validate_login_widget(data, "") is None


def test_init_data_gives_the_name_and_nothing_else():
    tg = parse_init_data(make_init_data(5, TOKEN, first_name="Анна"), TOKEN)
    assert (tg.id, tg.name) == (5, "Анна")
    assert parse_init_data("hash=bad", TOKEN) is None


# ---------- вход, выход, сессии ----------

def test_anonymous_visitor_gets_login_info_instead_of_an_error(client):
    data = client.get("/api/v1/me").json()
    assert data["authenticated"] is False and data["user"] is None
    assert data["login"] == {"enabled": True, "bot_username": config.TELEGRAM_BOT_USERNAME}
    assert client.get("/api/v1/me").headers["cache-control"] == "no-store"


def test_mini_app_login_creates_an_account_without_history_and_sets_a_httponly_cookie(client, session):
    response = client.post("/api/v1/auth/telegram", json={"init_data": make_init_data(1001, TOKEN, first_name="Анна")})
    data = response.json()
    assert data["authenticated"] and data["user"]["display_name"] == "Анна" and data["user"]["role"] == "user"
    assert data["user"]["history_enabled"] is False and data["user"]["consent_at"] is None  # история только по согласию
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=lax" in header  # без Secure: адрес в тестах http
    cookie = client.cookies.get(SESSION_COOKIE)
    assert cookie
    row = session.scalar(select(UserSession))
    assert row.token_hash == hashlib.sha256(cookie.encode()).hexdigest() and cookie not in row.token_hash  # в базе только хеш
    assert client.get("/api/v1/me").json()["user"]["display_name"] == "Анна"


def test_login_can_ask_for_history_consent_on_the_first_visit(client):
    assert login(client, 1002, history=True)["user"]["history_enabled"] is True


def test_bad_signature_or_missing_fields_do_not_log_anyone_in(client, monkeypatch):
    assert client.post("/api/v1/auth/telegram", json={"init_data": "hash=bad"}).status_code == 401
    assert client.post("/api/v1/auth/telegram", json={"init_data": make_init_data(1, TOKEN, auth_date=1)}).status_code == 401
    assert client.post("/api/v1/auth/telegram", json={"widget": {**make_login_widget_data(1, TOKEN), "id": "2"}}).status_code == 401
    assert client.post("/api/v1/auth/telegram", json={}).status_code == 422
    assert client.post("/api/v1/auth/telegram", json={"init_data": "x", "widget": {"id": 1}}).status_code == 422
    assert client.get("/api/v1/me").json()["authenticated"] is False
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    assert client.post("/api/v1/auth/telegram", json={"init_data": make_init_data(1, TOKEN)}).status_code == 503


def test_login_button_data_works_through_the_api(client):
    response = client.post("/api/v1/auth/telegram", json={"widget": make_login_widget_data(321, TOKEN, first_name="Борис")})
    assert response.status_code == 200 and response.json()["user"]["display_name"] == "Борис"


def test_callback_needs_the_nonce_cookie_so_a_foreign_link_cannot_log_the_visitor_in(client, session):
    nonce = "n" * 24
    query = make_login_widget_data(500, TOKEN)
    path = f"/api/v1/auth/telegram/callback/{nonce}"
    refused = client.get(path, params=query, follow_redirects=False)  # чужая ссылка: куки с nonce у посетителя нет
    assert refused.status_code == 303 and refused.headers["location"] == "/account?login=failed"
    assert client.cookies.get(SESSION_COOKIE) is None and session.scalar(select(User)) is None
    client.cookies.set("afisha_login_nonce", nonce)
    ok = client.get(path, params=query, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/account"
    assert client.get("/api/v1/me").json()["user"]["display_name"] == "Тест"
    client.cookies.set("afisha_login_nonce", "other" * 5)
    assert client.get(path, params={**query, "hash": "0" * 64}, follow_redirects=False).headers["location"] == "/account?login=failed"


def test_cross_site_login_post_is_refused_before_it_reaches_the_account_code(client):
    response = client.post("/api/v1/auth/telegram", json={"init_data": make_init_data(9, TOKEN)}, headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403 and client.cookies.get(SESSION_COOKIE) is None


def test_login_attempts_are_limited_per_address(client):
    codes = [client.post("/api/v1/auth/telegram", json={"init_data": "hash=bad"}).status_code for _ in range(22)]
    assert codes[:20] == [401] * 20 and 429 in codes[20:]


def test_logout_closes_the_session_and_clears_the_cookie(client, session):
    login(client)
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/me").json()["authenticated"] is False
    assert session.scalar(select(UserSession)) is None


def test_expired_session_is_ignored(client, session):
    login(client)
    row = session.scalar(select(UserSession))
    row.expires_at = now_local() - timedelta(minutes=1)
    session.commit()
    assert client.get("/api/v1/me").json()["authenticated"] is False
    assert client.get("/api/v1/me/history").status_code == 401


def test_a_person_has_at_most_ten_logins_at_once(client, session):
    for _ in range(13):
        client.post("/api/v1/auth/telegram", json={"init_data": make_init_data(1, TOKEN)})
    assert len(list(session.scalars(select(UserSession)))) == accounts.MAX_SESSIONS


def test_account_endpoints_need_a_login(client):
    for method, path in (("get", "/api/v1/me/history"), ("delete", "/api/v1/me/history"), ("get", "/api/v1/me/export"),
                         ("get", "/api/v1/me/searches"), ("get", "/api/v1/me/feedback"), ("delete", "/api/v1/me"),
                         ("get", "/api/v1/me/api-keys"), ("get", "/api/v1/ministry/stats")):
        assert getattr(client, method)(path).status_code == 401, path


# ---------- история посещений ----------

def test_history_is_off_until_the_person_agrees_and_records_a_card_once_a_day(client, add, session):
    event = future_event(add)
    login(client)
    client.get(f"/event/{event.id}")
    assert client.get("/api/v1/me/history").json() == {"enabled": False, "retention_days": config.HISTORY_RETENTION_DAYS, "total": 0, "items": []}
    assert client.post("/api/v1/me/history", json={"event_id": event.id}).json() == {"recorded": False}  # без согласия не пишется

    assert client.patch("/api/v1/me", json={"history_enabled": True}).json()["user"]["consent_at"]
    client.get(f"/event/{event.id}")
    client.get(f"/event/{event.id}")  # повтор за сутки
    page = client.get("/api/v1/me/history").json()
    assert page["total"] == 1 and page["items"][0]["title"] == event.title and page["items"][0]["kind"] == "view"
    assert client.post("/api/v1/me/history", json={"event_id": event.id, "kind": "ticket"}).json() == {"recorded": True}
    assert client.post("/api/v1/me/history", json={"event_id": event.id, "kind": "ticket"}).json() == {"recorded": False}
    assert client.get("/api/v1/me/history").json()["total"] == 2


def test_history_skips_unknown_and_unpublished_cards(client, add, session):
    event = future_event(add)
    login(client, history=True)
    event.publication = "draft"
    session.commit()
    assert client.post("/api/v1/me/history", json={"event_id": event.id}).json() == {"recorded": False}
    assert client.post("/api/v1/me/history", json={"event_id": 99999}).json() == {"recorded": False}
    assert client.post("/api/v1/me/history", json={"event_id": 0}).status_code == 422
    assert client.post("/api/v1/me/history", json={"event_id": event.id, "kind": "other"}).status_code == 422


def test_clear_and_withdrawing_consent_erase_the_history(client, add):
    event = future_event(add)
    login(client, history=True)
    client.get(f"/event/{event.id}")
    assert client.delete("/api/v1/me/history").json() == {"removed": 1}
    client.get(f"/event/{event.id}?again=1")
    client.post("/api/v1/me/history", json={"event_id": event.id, "kind": "ticket"})
    assert client.patch("/api/v1/me", json={"history_enabled": False}).json()["user"]["history_enabled"] is False
    assert client.get("/api/v1/me/history").json()["total"] == 0  # хранить без согласия нельзя


def test_history_older_than_the_retention_period_is_deleted(client, add, session):
    event = future_event(add)
    login(client, history=True)
    user = session.scalar(select(User))
    session.add_all([VisitHistory(user_id=user.id, event_id=event.id, kind="view", viewed_at=now_local() - timedelta(days=config.HISTORY_RETENTION_DAYS + 1)),
                     VisitHistory(user_id=user.id, event_id=event.id, kind="ticket", viewed_at=now_local() - timedelta(days=1))])
    session.commit()
    assert [i["kind"] for i in client.get("/api/v1/me/history").json()["items"]] == ["ticket"]


def test_history_shows_cards_that_have_left_the_afisha(client, add, session):
    event = future_event(add)
    login(client, history=True)
    client.get(f"/event/{event.id}")
    session.delete(event)
    session.commit()
    item = client.get("/api/v1/me/history").json()["items"][0]
    assert item["available"] is False and item["title"] == ""


def test_event_page_is_the_same_for_everyone_so_caches_stay_correct(client, add):
    event = future_event(add)
    anonymous = client.get(f"/event/{event.id}")
    login(client, history=True)
    member = client.get(f"/event/{event.id}")
    assert anonymous.content == member.content and anonymous.headers["etag"] == member.headers["etag"]


# ---------- сохранённые поиски ----------

def test_saved_searches_keep_only_known_conditions(client):
    login(client)
    created = client.post("/api/v1/me/searches", json={"name": "Джаз в Уфе", "filters": {"q": "джаз", "city": "Уфа", "price_max": 1000, "free": True, "kids": False}})
    assert created.status_code == 201 and created.json()["filters"] == {"q": "джаз", "city": "Уфа", "price_max": 1000, "free": True}
    assert [s["name"] for s in client.get("/api/v1/me/searches").json()] == ["Джаз в Уфе"]
    for bad in ({"filters": {"password": "x"}}, {"filters": {}}, {"filters": {"price_max": -1}}, {"filters": {"date_from": "завтра"}}, {"filters": {"city": 5}}):
        assert client.post("/api/v1/me/searches", json={"name": "x", **bad}).status_code == 422, bad


def test_saved_searches_are_private_and_limited(client, session):
    login(client, 1)
    first = client.post("/api/v1/me/searches", json={"name": "а", "filters": {"q": "а"}}).json()["id"]
    for number in range(accounts.MAX_SAVED_SEARCHES - 1):
        assert client.post("/api/v1/me/searches", json={"name": str(number), "filters": {"q": "б"}}).status_code == 201
    assert client.post("/api/v1/me/searches", json={"name": "лишний", "filters": {"q": "в"}}).status_code == 409
    client.post("/api/v1/auth/logout")
    login(client, 2)
    assert client.get("/api/v1/me/searches").json() == []
    assert client.delete(f"/api/v1/me/searches/{first}").status_code == 404  # чужой поиск не удалить
    assert session.get(SavedSearch, first) is not None


# ---------- подписки ----------

def test_subscribe_from_a_plain_browser_after_login_and_link_bot_subscriptions(client, add, session):
    future_event(add)  # город «Уфа» должен быть в афише
    session.add(Subscription(chat_id=1001, city="Салават", category="concert"))  # подписка из бота до входа
    session.commit()
    assert client.post("/api/v1/subscriptions", json={"city": "Уфа"}).status_code == 401  # без входа и без подписи нельзя
    user = login(client, 1001)["user"]
    assert session.scalar(select(Subscription).where(Subscription.category == "concert")).user_id == user["id"]
    created = client.post("/api/v1/subscriptions", json={"city": "Уфа"})
    assert created.status_code == 201, created.text
    assert {s["label"] for s in client.get("/api/v1/subscriptions").json()} == {"Салават · Концерт", "Уфа · любая категория"}
    assert {s.user_id for s in session.scalars(select(Subscription))} == {user["id"]}  # у обеих один владелец


# ---------- обращения и организатор ----------

def test_feedback_from_an_account_shows_status_and_reply_and_notifies_through_the_bot(client, add, session, admin):
    event = future_event(add)
    anonymous = client.post("/api/v1/feedback", json={"kind": "other", "message": "Анонимное обращение без входа.", "token": feedback.issue_token(time.time() - 10)})
    assert anonymous.status_code == 200
    make_role(client, admin, 2001, "organizer")
    me = login(client, 2001)["user"]
    assert me["role"] == "organizer"
    token = feedback.issue_token(time.time() - 10)
    sent = client.post("/api/v1/feedback", json={"kind": "organizer", "event_id": event.id, "message": "Перенесите концерт на 20:00, пожалуйста.", "token": token})
    assert sent.status_code == 200

    own = client.get("/api/v1/me/feedback").json()
    assert [(f["status"], f["status_label"], f["reply"]) for f in own] == [("new", "Принято", "")]
    inbox = client.get("/api/v1/admin/feedback?status=all", headers=admin).json()
    assert {f["author_role"] for f in inbox if f["user_id"]} == {"organizer"}

    item_id = own[0]["id"]
    assert client.patch(f"/api/v1/admin/feedback/{item_id}", json={"status": "in_progress"}, headers=admin).json()["status"] == "in_progress"
    assert feedback.pending_replies(session) == []  # ответа ещё нет
    done = client.patch(f"/api/v1/admin/feedback/{item_id}", json={"status": "processed", "reply": "Готово, время изменено."}, headers=admin).json()
    assert done["reply"] == "Готово, время изменено." and done["processed_by"] == "admin"
    own = client.get("/api/v1/me/feedback").json()[0]
    assert (own["status_label"], own["reply"]) == ("Обработано", "Готово, время изменено.")
    pending = feedback.pending_replies(session)
    assert [p.id for p in pending] == [item_id]
    pending[0].notified_at = now_local()
    session.commit()
    assert feedback.pending_replies(session) == []
    audit = client.get("/api/v1/admin/audit", headers=admin).json()
    assert "Готово" not in str(audit)  # текст ответа адресован автору и в журнал не копируется


def test_feedback_update_validation(client, admin, session):
    session.add(Feedback(kind="other", message="Тестовое обращение", message_hash="h"))
    session.commit()
    path = "/api/v1/admin/feedback/1"
    assert client.patch(path, json={}, headers=admin).status_code == 422
    assert client.patch(path, json={"status": "done"}, headers=admin).status_code == 422
    assert client.patch(path, json={"reply": "я" * (feedback.MAX_REPLY + 1)}, headers=admin).status_code == 422
    assert client.patch(path, json={"status": "processed"}, headers=admin).status_code == 200  # прежний порядок работает


# ---------- роли ----------

def test_roles_are_given_only_with_the_admin_key_and_can_be_prepared_before_the_first_login(client, session, admin):
    assert client.post("/api/v1/admin/users", json={"telegram_id": 3001, "role": "moderator"}).status_code == 401  # без ключа нельзя
    assert client.post("/api/v1/admin/users", json={"telegram_id": 3001, "role": "king"}, headers=admin).status_code == 422
    prepared = make_role(client, admin, 3001, "moderator")
    assert prepared["signed_in"] is False and prepared["role"] == "moderator"
    assert login(client, 3001)["user"]["role"] == "moderator"  # роль дождалась входа
    users = client.get("/api/v1/admin/users?role=moderator", headers=admin).json()
    assert [u["telegram_id"] for u in users] == [3001] and users[0]["signed_in"] is True
    assert client.get("/api/v1/admin/users", headers=admin, params={"q": "3001"}).json()[0]["telegram_id"] == 3001
    audit = client.get("/api/v1/admin/audit", headers=admin).json()
    assert any(e["action"] == "role" and e["changes"]["role"] == [None, "moderator"] for e in audit)


def test_a_moderator_account_works_in_the_admin_api_and_is_named_in_the_log(client, add, session, admin):
    event = future_event(add)
    make_role(client, admin, 3002, "moderator")
    user = login(client, 3002)["user"]
    assert client.get("/api/v1/admin/session").json() == {"actor": f"user:{user['id']}", "via": "account", "can_manage_users": False}
    assert client.get("/api/v1/admin/events").status_code == 200
    assert client.patch(f"/api/v1/admin/events/{event.id}", json={"title": "Новое название"}).status_code == 200
    log = client.get("/api/v1/admin/audit", headers=admin).json()
    assert any(e["actor"] == f"user:{user['id']}" and e["action"] in ("edit", "update") for e in log)
    # ключи Министерства и роли выдаёт только владелец ключа
    assert client.post("/api/v1/admin/users", json={"telegram_id": 5, "role": "ministry"}).status_code == 401
    assert client.get("/api/v1/admin/api-keys").status_code == 401


def test_other_roles_do_not_get_the_admin_api(client, admin):
    for number, role in enumerate(("user", "organizer", "ministry"), start=4000):
        make_role(client, admin, number, role)
        login(client, number)
        assert client.get("/api/v1/admin/events").status_code == 401, role  # ключа нет, а роль не «модератор»
        client.post("/api/v1/auth/logout")


def test_revoking_access_ends_logins_and_drops_the_role_at_once(client, session, admin):
    prepared = make_role(client, admin, 3003, "moderator")
    login(client, 3003)
    assert client.get("/api/v1/admin/events").status_code == 200
    revoked = client.post(f"/api/v1/admin/users/{prepared['id']}/revoke", headers=admin)
    assert revoked.status_code == 200 and revoked.json()["role"] == "user"
    assert client.get("/api/v1/me").json()["authenticated"] is False  # все входы закрыты
    assert client.get("/api/v1/admin/events").status_code == 401
    assert client.post("/api/v1/admin/users/9999/revoke", headers=admin).status_code == 404


# ---------- ключи Министерства ----------

def test_ministry_issues_read_only_keys_with_their_own_limit(client, session, admin):
    make_role(client, admin, 6001, "ministry")
    login(client, 6001)
    created = client.post("/api/v1/me/api-keys", json={"name": "Сайт министерства"})
    assert created.status_code == 201
    key = created.json()
    assert key["key"].startswith("afk_") and key["rate_limit"] == accounts.DEFAULT_KEY_LIMIT
    stored = session.scalar(select(ApiKey))
    assert stored.key_hash == hashlib.sha256(key["key"].encode()).hexdigest() and key["key"] not in (stored.key_hash, stored.key_prefix)
    assert "key" not in client.get("/api/v1/me/api-keys").json()[0]  # ключ показывается один раз
    client.post("/api/v1/auth/logout")  # дальше работаем как посторонний сайт: только заголовок, без куки

    header = {"X-Partner-Key": key["key"]}
    assert client.get("/api/v1/events", headers=header).status_code == 200
    assert client.post("/api/v1/events", json={"title": "Событие", "start_at": "2030-01-01T10:00:00", "source": "x"}, headers=header).status_code in (401, 503)  # писать им нельзя
    assert client.get("/api/v1/events", headers={"X-Partner-Key": "afk_wrong"}).status_code == 401
    assert client.get("/api/v1/events", headers={"X-Partner-Key": key["key"]}).headers["cache-control"].startswith("public")


def test_each_key_has_its_own_limit_and_is_counted_and_revoked(client, session, admin):
    make_role(client, admin, 6002, "ministry")
    owner = login(client, 6002)["user"]
    key = client.post("/api/v1/me/api-keys", json={"name": "Приложение"}).json()
    client.post("/api/v1/auth/logout")
    assert client.patch(f"/api/v1/admin/api-keys/{key['id']}", json={"rate_limit": 3}, headers=admin).json()["rate_limit"] == 3
    header = {"X-Partner-Key": key["key"]}
    codes = [client.get("/api/v1/cities", headers=header).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert client.get("/api/v1/cities").status_code == 200  # без ключа лимит общий, ключ чужих не задевает
    listed = client.get("/api/v1/admin/api-keys", headers=admin).json()
    assert listed[0]["requests_total"] == 3 and listed[0]["owner_user_id"] == owner["id"]  # отказанные запросы не считаются
    assert client.delete(f"/api/v1/admin/api-keys/{key['id']}", headers=admin).status_code == 204
    assert client.get("/api/v1/cities", headers=header).status_code == 401  # отозванный ключ не работает
    assert client.delete("/api/v1/admin/api-keys/9999", headers=admin).status_code == 404


def test_keyed_reads_do_not_use_the_shared_address_limit_but_wrong_keys_get_stopped(client, monkeypatch, admin):
    make_role(client, admin, 6003, "ministry")
    login(client, 6003)
    key = client.post("/api/v1/me/api-keys", json={"name": "k"}).json()["key"]
    client.post("/api/v1/auth/logout")
    monkeypatch.setattr(api_limiter, "limit", 3)
    assert [client.get("/api/v1/cities", headers={"X-Partner-Key": key}).status_code for _ in range(6)] == [200] * 6
    api_limiter.hits.clear()
    codes = [client.get("/api/v1/cities", headers={"X-Partner-Key": "afk_nope"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and 429 in codes[10:]  # подбор ключа останавливается, как подбор ключа модератора


def test_only_the_ministry_role_issues_keys_and_a_user_cannot_touch_foreign_keys(client, session, admin):
    make_role(client, admin, 6004, "organizer")
    login(client, 6004)
    assert client.post("/api/v1/me/api-keys", json={"name": "k"}).status_code == 403
    client.post("/api/v1/auth/logout")
    make_role(client, admin, 6005, "ministry")
    login(client, 6005)
    mine = client.post("/api/v1/me/api-keys", json={"name": "мой"}).json()
    for number in range(accounts.MAX_KEYS_PER_USER - 1):
        assert client.post("/api/v1/me/api-keys", json={"name": f"k{number}"}).status_code == 201
    assert client.post("/api/v1/me/api-keys", json={"name": "лишний"}).status_code == 409
    client.post("/api/v1/auth/logout")
    make_role(client, admin, 6006, "ministry")
    login(client, 6006)
    assert client.delete(f"/api/v1/me/api-keys/{mine['id']}").status_code == 404  # чужой ключ не отозвать
    client.post("/api/v1/auth/logout")
    login(client, 6005)
    assert client.delete(f"/api/v1/me/api-keys/{mine['id']}").status_code == 204
    assert session.get(ApiKey, mine["id"]).revoked_at is not None


def test_admin_can_issue_a_key_with_a_limit_for_a_known_owner(client, admin):
    assert client.post("/api/v1/admin/api-keys", json={"name": "для партнёра", "rate_limit": 50, "owner_telegram_id": 777}, headers=admin).status_code == 404
    make_role(client, admin, 777, "ministry")
    created = client.post("/api/v1/admin/api-keys", json={"name": "для партнёра", "rate_limit": 50, "owner_telegram_id": 777}, headers=admin)
    assert created.status_code == 201 and created.json()["rate_limit"] == 50 and created.json()["key"].startswith("afk_")
    assert client.post("/api/v1/admin/api-keys", json={"name": "x", "rate_limit": 10**6}, headers=admin).status_code == 422


def test_ministry_stats_are_counters_without_texts_or_people(client, add, session, admin):
    future_event(add)
    session.add(Feedback(kind="organizer", message="Секретный текст обращения", message_hash="1", status="new"))
    session.commit()
    make_role(client, admin, 6007, "ministry")
    login(client, 6007)
    stats = client.get("/api/v1/ministry/stats").json()
    assert stats["events_published"] == 1 and stats["feedback_total"] == 1
    assert stats["feedback_by_kind"] == {"organizer": 1} and stats["feedback_by_status"] == {"new": 1}
    assert "Секретный текст" not in client.get("/api/v1/ministry/stats").text


# ---------- экспорт и удаление ----------

def test_export_has_everything_stored_about_the_person_and_no_secrets(client, add, session, admin):
    event = future_event(add)
    login(client, 8001, history=True)
    client.get(f"/event/{event.id}")
    client.post("/api/v1/me/searches", json={"name": "поиск", "filters": {"q": "джаз"}})
    client.post("/api/v1/subscriptions", json={"city": "Уфа"})
    client.post("/api/v1/feedback", json={"kind": "question", "message": "Как добавить событие?", "contact": "me@example.org", "token": feedback.issue_token(time.time() - 10)})
    response = client.get("/api/v1/me/export")
    assert "attachment" in response.headers["content-disposition"]
    data = response.json()
    assert data["profile"]["telegram_id"] == 8001 and len(data["history"]) == 1 and data["saved_searches"][0]["filters"] == {"q": "джаз"}
    assert data["subscriptions"][0]["city"] == "Уфа" and data["feedback"][0]["message"] == "Как добавить событие?"
    assert "token_hash" not in response.text and client.cookies.get(SESSION_COOKIE) not in response.text


def test_export_includes_a_long_history_not_just_the_first_page(client, add, session):
    event = future_event(add)
    user = login(client, history=True)["user"]
    session.add_all([VisitHistory(user_id=user["id"], event_id=event.id, kind="view", viewed_at=now_local() - timedelta(minutes=n)) for n in range(450)])
    session.commit()
    assert len(client.get("/api/v1/me/export").json()["history"]) == 450


def test_deleting_the_account_erases_the_person_and_keeps_feedback_anonymous(client, add, session):
    event = future_event(add)
    user = login(client, 8002, history=True)["user"]
    client.get(f"/event/{event.id}")
    client.post("/api/v1/me/searches", json={"name": "поиск", "filters": {"q": "а"}})
    client.post("/api/v1/subscriptions", json={"city": "Уфа"})
    client.post("/api/v1/feedback", json={"kind": "question", "message": "Вопрос про афишу, пожалуйста.", "contact": "me@example.org", "token": feedback.issue_token(time.time() - 10)})
    session.add(Subscription(chat_id=8002, city="Салават", category=""))  # подписка из бота тоже стирается
    session.commit()
    assert client.delete("/api/v1/me").status_code == 204
    assert client.get("/api/v1/me").json()["authenticated"] is False
    for model in (User, UserSession, VisitHistory, SavedSearch, Subscription):
        assert session.scalar(select(model)) is None, model.__name__
    kept = session.scalar(select(Feedback))
    assert kept.user_id is None and kept.contact == "" and kept.message.startswith("Вопрос")
    again = login(client, 8002)["user"]  # вход после удаления — новый аккаунт: ни согласия, ни истории, ни прежнего номера
    assert again["id"] != user["id"] and again["history_enabled"] is False and client.get("/api/v1/me/history").json()["total"] == 0


# ---------- страница и защита ----------

def test_account_page_is_served_without_caching_or_indexing_and_csp_allows_only_the_login_frame(client):
    response = client.get("/account")
    assert response.status_code == 200 and "noindex" in response.headers["x-robots-tag"] and response.headers["cache-control"] == "no-cache"
    csp = response.headers["content-security-policy"]
    assert "frame-src https://oauth.telegram.org" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "<script>" not in response.text  # встроенные скрипты запрещены политикой


# ---------- миграция ----------

def test_migration_0005_upgrades_a_database_from_the_previous_version(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'afisha.db'}")
    monkeypatch.setattr(db, "engine", engine)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:  # вернуть схему к версии 0004: без таблиц аккаунтов и новых столбцов
        for table in ("users", "user_sessions", "visit_history", "saved_searches", "api_keys"):
            conn.execute(text(f"DROP TABLE {table}"))
        for index in ("ix_subscriptions_user_id", "ix_feedback_user_id"):
            conn.execute(text(f"DROP INDEX {index}"))
        for table, column in (("subscriptions", "user_id"), ("feedback", "user_id"), ("feedback", "reply"),
                              ("feedback", "replied_at"), ("feedback", "notified_at")):
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('0004')"))
    assert db.run_migrations(engine) == "upgraded"
    found = inspect(engine)
    assert {"users", "user_sessions", "visit_history", "saved_searches", "api_keys"} <= set(found.get_table_names())
    assert {"user_id", "reply", "replied_at", "notified_at"} <= {c["name"] for c in found.get_columns("feedback")}
    assert "user_id" in {c["name"] for c in found.get_columns("subscriptions")}
    assert any(i["unique"] and i["column_names"] == ["telegram_id"] for i in found.get_indexes("users"))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0006"  # до последней миграции
    assert "bot_dialogs" in set(found.get_table_names())
    assert db.run_migrations(engine) == "upgraded"  # повторный запуск ничего не ломает


# ---------- общий слой интерфейса: панель разделов, «Следить», «Поделиться» ----------

def test_every_public_page_gets_the_shared_navigation_and_no_inline_scripts(client, add):
    event = future_event(add)
    for path in ("/", "/account", "/feedback", f"/event/{event.id}", "/plan"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert "/static/ui.css" in page.text and "/static/ui.js" in page.text, path
        assert "<script>" not in page.text.replace('<script type="application/ld+json">', ""), path  # политика безопасности запрещает встроенные
    assert "/static/ui.js" not in client.get("/admin").text  # внутренняя страница без общей панели
    for asset in ("ui.css", "ui.js"):
        assert client.get(f"/static/{asset}").status_code == 200


def test_event_card_has_one_click_follow_and_share_and_cancelled_events_have_no_follow(client, add):
    event = future_event(add)
    page = client.get(f"/event/{event.id}").text
    assert 'data-follow data-city="Уфа" data-category="concert"' in page and "data-share" in page
    assert 'data-visit="ticket"' in page or "Источник и билеты" not in page  # ссылку на билеты по-прежнему отслеживает visit.js
    event.status = "cancelled"
    assert "data-follow" not in client.get(f"/event/{event.id}").text