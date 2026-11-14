"""Аналитика: профиль и рекомендации пользователя, сводка и CSV для администратора."""
import csv
import io
from datetime import datetime

from app import accounts, analytics
from app.models import SavedSearch, Subscription, VisitHistory
from app.telegram_auth import TelegramUser

NOW = datetime(2030, 1, 5, 12, 0)


def make_events(add):
    e = {}
    e["drama"], _ = add("Спектакль «Ревизор»", datetime(2030, 1, 8, 19, 0), category="theatre", city="Уфа", venue="Театр драмы", price_min=500)
    e["ballet"], _ = add("Балет «Щелкунчик»", datetime(2030, 1, 9, 19, 0), category="theatre", city="Уфа", venue="Театр оперы", price_min=800)
    e["seen"], _ = add("Спектакль «Гамлет»", datetime(2030, 1, 10, 19, 0), category="theatre", city="Уфа", venue="Театр драмы", price_min=600)
    e["concert"], _ = add("Концерт оркестра", datetime(2030, 1, 8, 19, 0), category="concert", city="Уфа", price_min=400)
    e["far"], _ = add("Спектакль в Сибае", datetime(2030, 1, 8, 18, 0), category="theatre", city="Сибай", price_min=300)
    e["past"], _ = add("Прошедший спектакль", datetime(2030, 1, 1, 19, 0), category="theatre", city="Уфа", price_min=500)
    return e


def user_with_history(session, events, tg_id=101):
    user, _ = accounts.sign_in(session, TelegramUser(tg_id, "Зрительница"), history=True)
    for key in ("seen", "past"):
        session.add(VisitHistory(user_id=user.id, event_id=events[key].id, kind="view", viewed_at=datetime(2030, 1, 2, 10, 0)))
    session.add(VisitHistory(user_id=user.id, event_id=events["seen"].id, kind="ticket", viewed_at=datetime(2030, 1, 2, 10, 5)))
    session.commit()
    return user


def test_recommendations_follow_the_profile_and_skip_seen_events(session, add):
    events = make_events(add)
    user = user_with_history(session, events)
    profile = analytics.build_profile(session, user, NOW)
    assert profile.categories.most_common(1)[0][0] == "theatre" and profile.cities.most_common(1)[0][0] == "Уфа"
    assert profile.tickets == 1 and profile.views == 2 and len(profile.seen) == 2
    picks = analytics.recommend(session, user, NOW)
    titles = [e.title for e, _ in picks]
    assert "Спектакль «Гамлет»" not in titles and "Прошедший спектакль" not in titles  # уже открывал / прошло
    assert titles[0] == "Спектакль «Ревизор»"  # театр, Уфа и та же площадка, где уже бывала
    reasons = dict((e.title, why) for e, why in picks)["Спектакль «Ревизор»"]
    assert any("Театр" in r for r in reasons) and len(reasons) <= 2
    assert titles.index("Спектакль «Ревизор»") < titles.index("Концерт оркестра") if "Концерт оркестра" in titles else True


def test_subscriptions_and_saved_searches_count_without_history(session, add):
    make_events(add)
    user, _ = accounts.sign_in(session, TelegramUser(202, "Без истории"))
    session.add(Subscription(chat_id=202, user_id=user.id, city="Уфа", category="concert"))
    session.add(SavedSearch(user_id=user.id, name="Бесплатно", filters={"free": True, "city": "Уфа"}))
    session.commit()
    profile = analytics.build_profile(session, user, NOW)
    assert profile.categories["concert"] == analytics.WEIGHT_SUB and profile.free_interest
    assert analytics.recommend(session, user, NOW)[0][0].title == "Концерт оркестра"


def test_new_user_gets_popular_events(session, add):
    events = make_events(add)
    fan = user_with_history(session, events)
    session.add(VisitHistory(user_id=fan.id, event_id=events["concert"].id, kind="view", viewed_at=datetime(2030, 1, 4, 10, 0)))
    session.commit()
    newbie, _ = accounts.sign_in(session, TelegramUser(303, "Новичок"))
    picks = analytics.recommend(session, newbie, NOW)
    assert picks[0][0].title in ("Концерт оркестра", "Спектакль «Гамлет»") and "популярно" in picks[0][1][0]


def test_overview_and_csv(session, add):
    events = make_events(add)
    user_with_history(session, events)
    session.add(Subscription(chat_id=999, city="Уфа", category="theatre"))
    accounts.sign_in(session, TelegramUser(404, "=HYPERLINK(\"evil\")"))
    session.commit()
    data = analytics.overview(session, NOW)
    assert data["users"]["total"] == 2 and data["engagement_30d"]["ticket_clicks"] == 1
    assert data["engagement_30d"]["top_events"][0]["title"] == "Спектакль «Гамлет»"
    assert data["subscriptions"]["total"] == 1 and len(data["engagement_30d"]["daily_visits_14d"]) == 14
    text = analytics.to_csv(analytics.users_rows(session, NOW))
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
    assert rows[0][:3] == ["№ аккаунта", "Имя", "Роль"] and len(rows) == 3
    assert rows[1][12] == "Театр" and rows[1][13] == "Уфа"
    assert rows[2][1].startswith("'=")  # формула из имени не исполнится в Excel
    assert "404" not in text  # номер Telegram не выгружается


def test_endpoints(client, session, add, admin_key):
    events = make_events(add)
    assert client.get("/api/v1/me/insights").status_code == 401
    user = user_with_history(session, events, tg_id=505)
    client.cookies.set(accounts.SESSION_COOKIE, accounts.start_session(session, user))
    body = client.get("/api/v1/me/insights").json()
    assert body["personal"] is True and body["profile"]["top_categories"][0]["label"] == "Театр"
    assert all(item["reasons"] for item in body["recommendations"])
    client.cookies.clear()
    assert client.get("/api/v1/admin/analytics").status_code == 401
    assert client.get("/api/v1/admin/analytics", headers=admin_key).json()["users"]["total"] == 1
    res = client.get("/api/v1/admin/analytics/users.csv", headers=admin_key)
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    assert "attachment" in res.headers["content-disposition"]
