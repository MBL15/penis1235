"""Бот: выбор даты и цены кнопками, источник и билеты отдельно, пустая выдача с подсказкой; подсказка в API."""
from datetime import timedelta

import pytest

from app import bot
from app.hints import describe_constraints, empty_hint
from app.search import SearchFilters

from app.config import now_local

# Бот и API считают «сейчас» по настоящим часам, поэтому события заводим относительно них, а не от фиксированного NOW
NOW = now_local().replace(hour=9, minute=0, second=0, microsecond=0)


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_kinds_for_chosen_date_and_price_are_parsed_strictly():
    f = bot.quick_filters("date_2026-11-14", "Уфа")
    assert f.date_from == f.date_to and f.date_from.isoformat() == "2026-11-14" and f.city == "Уфа"
    assert bot.quick_filters("price_1000", None).price_max == 1000
    for bad in ("date_", "date_2026-99-99", "date_abc", "price_", "price_x", "cat_unknown", "nonsense", "date_2026-11-14;drop"):
        assert not bot.valid_kind(bad), bad
    for good in ("today", "cat_concert", "date_2026-11-14", "price_500"):
        assert bot.valid_kind(good), good
    assert "14.11.2026" in bot.kind_title("date_2026-11-14") and "до 500" in bot.kind_title("price_500")


def test_date_and_price_keyboards_fit_telegram_limits():
    dates = buttons(bot.dates_markup())
    assert sum(1 for b in dates if (b.callback_data or "").startswith("l:date_")) == bot.DATE_DAYS
    prices = buttons(bot.prices_markup())
    assert [b.callback_data for b in prices][:len(bot.PRICE_STEPS)] == [f"l:price_{s}:0" for s in bot.PRICE_STEPS]
    assert all(len((b.callback_data or "").encode()) <= 64 for b in dates + prices)


def test_empty_list_says_what_to_relax(session, add):
    add("Концерт «Вечер романсов»", NOW + timedelta(days=3, hours=3), city="Стерлитамак", category="concert", price_min=500)
    view = bot.list_view(session, "price_1000", "Уфа")  # в Уфе ничего нет, а в республике есть
    assert "Ничего не нашлось" in view.text and "по городу" in view.text and "нашлось 1" in view.text
    assert "Вечер романсов" in view.text  # названо ближайшее событие


def test_hint_names_the_single_condition_that_blocks_and_counts_events(session, add):
    add("Балет «Лебединое озеро»", NOW + timedelta(days=2, hours=4), city="Уфа", category="theatre", price_min=2500)
    f = SearchFilters(city="Уфа", category="theatre", price_max=1000)
    hint = empty_hint(session, f, NOW)
    assert "по цене" in hint and "нашлось 1" in hint and "до 1000" in hint and "город Уфа" in hint
    assert "по цене" not in empty_hint(session, SearchFilters(city="Салават"), NOW)  # другое условие названо верно
    nothing = empty_hint(session, SearchFilters(city="Салават", category="concert"), NOW)  # снятие любого одного условия не помогает
    assert "пока нет" in nothing
    assert describe_constraints(SearchFilters(free_only=True, kids=True)) == ["бесплатно", "детям"]


def test_event_card_separates_tickets_and_sources(session, add):
    event, _ = add("Концерт «Вечер романсов»", NOW + timedelta(days=3, hours=3), city="Уфа", category="concert",
                   url="https://tickets.example/1", source="culture.ru", source_url="https://culture.example/1")
    texts = [b.text for b in buttons(bot.event_view(session, event.id).markup)]
    assert "🎟 Билеты и организатор" in texts and any(t.startswith("🔗 Источник: culture.ru") for t in texts)
    assert "Концерт" in bot.event_view(session, event.id).text  # категория в карточке


def test_api_returns_hint_only_for_an_empty_first_page(client, add):
    event, _ = add("Балет «Лебединое озеро»", NOW + timedelta(days=2, hours=4), city="Уфа", category="theatre", price_min=2500)
    empty = client.get("/api/v1/events", params={"city": "Уфа", "price_max": 1000, "when": "week"}).json()
    assert empty["total"] == 0 and empty["hint"] and "цене" in empty["hint"]
    found = client.get("/api/v1/events", params={"city": "Уфа"}).json()
    assert found["total"] == 1 and found["hint"] is None
