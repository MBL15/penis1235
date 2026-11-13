"""Планировщик «Выходной день»: 2–4 события без пересечений, дорога, стоимость, объяснение несовместимых ограничений."""
from datetime import date, datetime, time, timedelta

import pytest

from app.config import now_local
from app.planner import PlanRequest, parse_budget_text, plan_weekend, travel_minutes

TODAY = now_local().date()
DAY = TODAY + timedelta(days=(5 - TODAY.weekday()) % 7 or 7)  # ближайшая суббота


def at(hour, minute=0):
    return datetime.combine(DAY, time(hour, minute))


def put(add, title, start, *, minutes=90, price=500, venue=None, city="Уфа", age=None, category="concert", **kw):
    return add(title, start, end_at=start + timedelta(minutes=minutes), price_min=price, price_max=price, city=city,
               venue=venue or f"Зал «{title}»", age_limit=age, category=category, url=f"https://example.org/{abs(hash(title)) % 10**6}", **kw)[0]


def titles(result):
    return [i.event.title for i in result.items]


def test_three_events_in_order_without_overlap_with_travel_and_total(session, add):
    put(add, "Утренний спектакль", at(10), minutes=90, price=400, venue="Театр")
    put(add, "Выставка", at(12), minutes=60, price=200, venue="Музей")
    put(add, "Вечерний концерт", at(15), minutes=120, price=800, venue="Филармония")
    put(add, "Параллельный концерт", at(10, 30), minutes=120, price=300, venue="Клуб")  # пересекается с утренним
    result = plan_weekend(session, PlanRequest(day=DAY, adults=2, budget=3000))
    assert result.feasible and len(result.items) == 3
    starts = [i.start for i in result.items]
    assert starts == sorted(starts) and all(a.end + timedelta(minutes=10) <= b.start for a, b in zip(result.items, result.items[1:]))
    assert result.items[0].travel_min == 0 and all(i.travel_min == 20 for i in result.items[1:])  # координат нет: 20 минут по городу
    assert result.total_cost == (400 + 200 + 800) * 2 <= 3000 and result.total_travel_min == 40
    assert "стоимость" in result.message


def test_cheapest_combination_is_chosen_when_several_fit(session, add):
    put(add, "Дорогой А", at(10), price=2000, venue="А")
    put(add, "Дешёвый Б", at(10), price=100, venue="Б")
    put(add, "Дорогой В", at(14), price=1500, venue="В")
    put(add, "Дешёвый Г", at(14), price=150, venue="Г")
    result = plan_weekend(session, PlanRequest(day=DAY, events=2))
    assert set(titles(result)) == {"Дешёвый Б", "Дешёвый Г"}


def test_children_get_only_suitable_events_by_the_youngest_age(session, add):
    put(add, "Кукольный театр", at(10), age=0, venue="Кукольный", category="kids")
    put(add, "Сказка", at(13), age=3, venue="Сказочный", category="kids")
    put(add, "Триллер", at(16), age=16, venue="Кино")
    put(add, "Спектакль без возраста", at(19), age=None, venue="Драма")
    ok = plan_weekend(session, PlanRequest(day=DAY, children=[8, 3], events=2))
    assert set(titles(ok)) == {"Кукольный театр", "Сказка"}  # 3-летнему подходят 0+ и 3+; 16+ и «без возраста» не берём
    small = plan_weekend(session, PlanRequest(day=DAY, children=[2], events=2))
    assert not small.feasible  # двухлетнему подходит одно событие 0+, а нужно минимум два


def test_distance_by_coordinates(session, add):
    near_a = put(add, "Рядом А", at(10), venue="А", lat=54.7388, lon=55.9721)
    near_b = put(add, "Рядом Б", at(13), venue="Б", lat=54.7431, lon=55.9787)
    far = put(add, "Далеко", at(16), venue="В", lat=54.9000, lon=56.2000)
    assert travel_minutes(near_a, near_b) < 15 and travel_minutes(near_a, far) > 30
    plan = plan_weekend(session, PlanRequest(day=DAY, events=2, max_travel_min=15))
    assert set(titles(plan)) == {"Рядом А", "Рядом Б"}


def test_budget_too_low_names_the_budget_and_the_minimal_change(session, add):
    put(add, "Первое", at(10), price=1000, venue="А")
    put(add, "Второе", at(14), price=1500, venue="Б")
    result = plan_weekend(session, PlanRequest(day=DAY, adults=1, budget=1000, events=2))
    assert not result.feasible and "не получается" in result.message
    assert any("поднять бюджет с 1000 до 2500" in s.lower() for s in result.suggestions)


def test_window_too_narrow_suggests_a_wider_window(session, add):
    put(add, "Утро", at(9), price=100, venue="А")
    put(add, "Вечер", at(20), minutes=90, price=100, venue="Б")
    result = plan_weekend(session, PlanRequest(day=DAY, window_from=time(10), window_to=time(18), events=2))
    assert not result.feasible
    assert any("расширить окно времени до 09:00–21:30" in s.lower() for s in result.suggestions)


def test_long_travel_between_cities_names_the_travel_limit(session, add):
    put(add, "В Уфе", at(10), price=100, venue="А", city="Уфа")
    put(add, "В Стерлитамаке", at(16), price=100, venue="Б", city="Стерлитамак")
    result = plan_weekend(session, PlanRequest(day=DAY, events=2, max_travel_min=40))
    assert not result.feasible and any("дорогу до 90 мин" in s.lower() for s in result.suggestions)


def test_fewer_events_than_asked_are_returned_with_an_explanation(session, add):
    put(add, "Одно", at(10), price=100, venue="А")
    put(add, "Два", at(14), price=100, venue="Б")
    result = plan_weekend(session, PlanRequest(day=DAY, events=3))
    assert result.feasible and len(result.items) == 2 and "2 из 3" in result.message


def test_no_events_that_day_and_events_without_time_are_skipped(session, add):
    assert "подходящих событий нет" in plan_weekend(session, PlanRequest(day=DAY)).message
    add("Без времени", datetime.combine(DAY, time(0, 0)), price_min=0, city="Уфа", venue="А")
    assert not plan_weekend(session, PlanRequest(day=DAY)).items  # 00:00 — время не указано, в расписание не ставим


def test_unknown_price_and_estimated_end_are_reported(session, add):
    put(add, "Без цены", at(10), venue="А", price=None, duration_min=None)
    put(add, "С ценой", at(14), venue="Б", price=300)
    result = plan_weekend(session, PlanRequest(day=DAY, events=2))
    assert result.feasible and "Цена неизвестна у: Без цены" in result.message and result.total_cost == 300


def test_budget_text_parsing():
    assert parse_budget_text("до 2000 рублей на двоих") == (2000, 2)
    assert parse_budget_text("1 500 ₽") == (1500, None) and parse_budget_text("без бюджета") == (None, None)


def test_plan_api_returns_plan_and_validates_input(client, add):
    put(add, "Утро", at(10), price=100, venue="А")
    put(add, "День", at(14), price=200, venue="Б")
    body = {"day": DAY.isoformat(), "adults": 2, "budget": 1000, "events": 2, "city": "Уфа"}
    out = client.post("/api/v1/plan", json=body).json()
    assert out["feasible"] and [i["event"]["title"] for i in out["items"]] == ["Утро", "День"]
    assert out["total_cost"] == 600 and out["items"][1]["travel_min"] == 20 and "бюджет 1000" in " ".join(out["constraints"])
    tight = client.post("/api/v1/plan", json={**body, "budget": 100}).json()
    assert not tight["feasible"] and any("поднять бюджет" in s.lower() for s in tight["suggestions"])
    assert client.post("/api/v1/plan", json={**body, "day": (TODAY - timedelta(days=1)).isoformat()}).status_code == 422
    assert client.post("/api/v1/plan", json={**body, "window_from": "22:00", "window_to": "10:00"}).status_code == 422
    assert client.post("/api/v1/plan", json={**body, "children": [30]}).status_code == 422
    assert client.post("/api/v1/plan", json={**body, "city": "Атлантида"}).status_code == 422
    assert client.post("/api/v1/plan", json={**body, "events": 9}).status_code == 422
