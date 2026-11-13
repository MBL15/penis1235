from datetime import date, datetime

import pytest

from app.search import SearchFilters, facets, resolve_when, search

from .conftest import NOW  # noqa: E402  (суббота, 15:00)


@pytest.fixture
def events(add):
    add("Балет «Лебединое озеро»", datetime(2026, 11, 8, 18, 30), category="theatre", price_min=600, price_max=3000, age_limit=6)
    add("Кукольный спектакль «Три медведя»", datetime(2026, 11, 8, 12, 0), category="kids", price_min=300, price_max=500)
    add("Выставка «Орнамент»", datetime(2026, 11, 1), end_at=datetime(2026, 11, 20, 20, 0), category="exhibition",
        price_min=100, price_max=200)
    add("Концерт «Осенний бал»", datetime(2026, 11, 6, 19, 0), category="concert", price_min=300)  # прошло
    add("Спектакль «Вишнёвый сад»", datetime(2026, 11, 9, 18, 30), category="theatre", price_min=500, status="cancelled")
    add("Лекция об истории города", datetime(2026, 11, 10, 18, 0), category="lecture", price_min=0, price_max=0, age_limit=12)
    add("Концерт в Салавате", datetime(2026, 11, 11, 19, 0), city="Салават", category="concert")  # цена неизвестна
    add("Утренний забег", datetime(2026, 11, 7, 10, 0), category="sport", price_min=0, price_max=0)  # уже прошёл
    add("Вечерний джаз", datetime(2026, 11, 7, 19, 0), category="concert", price_min=500)


def titles(session, **kw):
    found, _ = search(session, SearchFilters(**kw), NOW)
    return {e.title for e in found}


def test_default_hides_past_and_cancelled(session, events):
    result = titles(session)
    assert "Концерт «Осенний бал»" not in result
    assert "Утренний забег" not in result
    assert "Спектакль «Вишнёвый сад»" not in result
    assert {"Выставка «Орнамент»", "Вечерний джаз", "Лекция об истории города"} <= result


def test_statuses_and_include_past(session, events):
    assert titles(session, statuses=["cancelled"]) == {"Спектакль «Вишнёвый сад»"}
    assert "Спектакль «Вишнёвый сад»" in titles(session, statuses=["all"])
    assert "Концерт «Осенний бал»" in titles(session, include_past=True)


def test_date_range_includes_multi_day_events_overlapping_it(session, events):
    sunday = titles(session, date_from=date(2026, 11, 8), date_to=date(2026, 11, 8))
    assert sunday == {"Балет «Лебединое озеро»", "Кукольный спектакль «Три медведя»", "Выставка «Орнамент»"}


def test_price_free_age_and_kids_filters(session, events):
    # Цена неизвестна → в выдачу «до 400 ₽» не попадает: мы не можем это подтвердить.
    assert titles(session, price_max=400) == {
        "Кукольный спектакль «Три медведя»", "Выставка «Орнамент»", "Лекция об истории города"}
    assert titles(session, free_only=True) == {"Лекция об истории города"}
    young = titles(session, age=5)
    assert "Балет «Лебединое озеро»" not in young and "Лекция об истории города" not in young
    assert "Кукольный спектакль «Три медведя»" in young
    assert titles(session, kids=True) == {"Кукольный спектакль «Три медведя»"}


def test_city_category_and_text_search_with_cyrillic(session, events):
    assert titles(session, city="в Салавате") == {"Концерт в Салавате"}
    assert titles(session, category="Театр") == {"Балет «Лебединое озеро»"}  # русское название тоже понимается
    assert titles(session, category="theatre,lecture") == {"Балет «Лебединое озеро»", "Лекция об истории города"}
    assert titles(session, category=" theatre , ") == {"Балет «Лебединое озеро»"}
    assert titles(session, q="спектакли") == {"Кукольный спектакль «Три медведя»"}  # «Вишнёвый сад» отменён
    assert titles(session, q="ЛЕБЕДИНОЕ озеро") == {"Балет «Лебединое озеро»"}
    assert titles(session, q="несуществующее") == set()


def test_ongoing_exhibitions_come_after_one_off_events(session, events):
    found, _ = search(session, SearchFilters(limit=50), NOW)
    order = [e.title for e in found]
    assert order[-1] == "Выставка «Орнамент»"  # идёт с 1 ноября — не должна быть в начале афиши
    assert order[0] == "Вечерний джаз"  # ближайшее разовое событие


def test_sort_pagination_and_total(session, events):
    found, total = search(session, SearchFilters(sort="price", limit=2), NOW)
    assert total == 6 and len(found) == 2
    assert [e.title for e in found] == ["Лекция об истории города", "Выставка «Орнамент»"]  # 0 ₽, потом 100 ₽
    page2, _ = search(session, SearchFilters(sort="price", limit=2, offset=2), NOW)
    assert not {e.id for e in found} & {e.id for e in page2}


def test_facets_count_only_current_events(session, events):
    data = facets(session, NOW)
    assert {"name": "Салават", "count": 1} in data["cities"]
    assert sum(c["count"] for c in data["cities"]) == 6


@pytest.mark.parametrize(
    "today, when, expected",
    [
        (date(2026, 11, 4), "weekend", (date(2026, 11, 7), date(2026, 11, 8))),  # среда
        (date(2026, 11, 7), "weekend", (date(2026, 11, 7), date(2026, 11, 8))),  # суббота
        (date(2026, 11, 8), "weekend", (date(2026, 11, 8), date(2026, 11, 8))),  # воскресенье
        (date(2026, 11, 7), "tomorrow", (date(2026, 11, 8), date(2026, 11, 8))),
        (date(2026, 11, 7), "week", (date(2026, 11, 7), date(2026, 11, 13))),
    ],
)
def test_resolve_when(today, when, expected):
    assert resolve_when(when, today) == expected
