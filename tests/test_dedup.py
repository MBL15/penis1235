from datetime import datetime

from sqlalchemy import func, select

from app.models import Event
from app.normalize import canonical_category, canonical_city, find_city_in_text, parse_age, title_key
from app.schemas import EventIn

SAT = datetime(2026, 11, 8, 18, 30)


def count(session):
    return session.scalar(select(func.count()).select_from(Event))


def test_normalize_helpers():
    assert canonical_city("в Уфе") == "Уфа"
    assert canonical_city("Стерлитамаке") == "Стерлитамак"
    assert canonical_city("Неведомск") == "Неведомск"
    assert canonical_category("Театр") == "theatre"
    assert canonical_category(None, "Джазовый вечер") == "concert"
    assert canonical_category("что-то", "ничего") == "other"
    assert parse_age("12+") == 12
    assert parse_age(None) is None and parse_age("0+") == 0 and parse_age("без ограничений") is None
    assert title_key("Спектакль «Золотой ключик»") == title_key("Золотой ключик")


def test_same_event_from_two_sources_is_merged(session, add):
    first, outcome1 = add("Балет «Лебединое озеро»", SAT, source="opera.ru", venue="Театр оперы",
                          price_min=600, price_max=3000, description="Коротко.")
    second, outcome2 = add("Лебединое озеро. Балет П. И. Чайковского", SAT, source="afisha.ru", venue="Театр оперы",
                           price_min=700, price_max=3500, description="Балет в двух действиях, около трёх часов.")
    assert (outcome1, outcome2) == ("created", "merged")
    assert first.id == second.id
    assert count(session) == 1
    assert [s["name"] for s in second.sources] == ["opera.ru", "afisha.ru"]
    assert second.description.startswith("Балет в двух действиях")  # берём более полное описание
    assert (second.price_min, second.price_max) == (600, 3500)  # честный диапазон при расхождении


def test_cancelled_status_wins_over_scheduled(session, add):
    add("Спектакль «Вишнёвый сад»", SAT, source="a", status="scheduled")
    event, outcome = add("Вишнёвый сад", SAT, source="b", status="cancelled")
    assert outcome == "merged"
    assert event.status == "cancelled"


def test_resync_of_one_source_does_not_erase_cancellation_from_another(session, add):
    add("Спектакль «Вишнёвый сад»", SAT, source="a", status="scheduled")
    add("Вишнёвый сад", SAT, source="b", status="cancelled")
    event, _ = add("Спектакль «Вишнёвый сад»", SAT, source="a", status="scheduled")  # свежая выгрузка A
    assert event.status == "cancelled"
    event, _ = add("Вишнёвый сад", SAT, source="b", status="scheduled")  # сам B снял отмену
    assert event.status == "scheduled"


def test_legacy_sources_without_status_keep_the_current_status(session, add):
    event, _ = add("Концерт оркестра", SAT, source="a", status="cancelled")
    event.sources = [{"name": "a", "url": ""}, {"name": "b", "url": ""}]  # как в базе до появления статуса в источниках
    session.commit()
    event, _ = add("Концерт оркестра", SAT, source="a", status="scheduled")
    assert event.status == "cancelled"  # b по-прежнему считается сообщившим об отмене


def test_city_comes_from_the_named_city_not_from_a_street():
    assert find_city_in_text("пр. Салавата Юлаева, 1, Уфа") == "Уфа"
    assert find_city_in_text("ул. Октябрьской революции, 3, Уфа") == "Уфа"
    assert find_city_in_text("г. Салават, ул. Ленина") == "Салават"
    assert find_city_in_text("Концерт в Салавате") == "Салават"  # форма слова всё ещё понимается по началу
    assert canonical_city("пр. Салавата Юлаева, Уфа") == "Уфа"


def test_same_source_resend_is_unchanged_and_refresh_applies_new_data(session, add):
    add("Концерт оркестра", SAT, source="a", price_min=300, url="https://x.test/1")
    _, outcome = add("Концерт оркестра", SAT, source="a", price_min=300, url="https://x.test/1")
    assert outcome == "unchanged"
    event, outcome = add("Концерт оркестра", SAT, source="a", price_min=400, url="https://x.test/1", status="sold_out")
    assert outcome == "merged"
    assert (event.price_min, event.status) == (400, "sold_out")
    assert count(session) == 1


def test_url_match_follows_a_moved_date(session, add):
    event, _ = add("Концерт оркестра", SAT, source="a", url="https://x.test/9")
    moved, outcome = add("Концерт оркестра", datetime(2026, 11, 15, 19, 0), source="a", url="https://x.test/9")
    assert outcome == "merged"
    assert moved.id == event.id
    assert moved.start_at == datetime(2026, 11, 15, 19, 0)
    assert count(session) == 1


def test_matinee_and_evening_shows_stay_separate(session, add):
    add("Спектакль «Три медведя»", datetime(2026, 11, 8, 12, 0), source="a", venue="Театр кукол")
    _, outcome = add("Спектакль «Три медведя»", datetime(2026, 11, 8, 18, 0), source="b", venue="Театр кукол")
    assert outcome == "created"
    assert count(session) == 2


def test_different_city_or_title_is_not_a_duplicate(session, add):
    add("Концерт «Осенний вальс»", SAT, source="a", city="Уфа")
    _, other_city = add("Концерт «Осенний вальс»", SAT, source="b", city="Салават")
    _, other_title = add("Джазовый вечер", SAT, source="b", city="Уфа")
    assert (other_city, other_title) == ("created", "created")
    assert count(session) == 3


def test_titles_differing_only_by_a_number_are_different_events(session, add):
    add("Лекция 1", SAT, source="a", venue="Библиотека")
    _, outcome = add("Лекция 2", SAT, source="b", venue="Библиотека")
    assert outcome == "created"
    _, outcome = add("Лекция 2", SAT, source="c", venue="Библиотека")  # а повтор того же — дубль
    assert outcome == "merged"
    assert count(session) == 2


def test_midnight_time_is_filled_from_other_source(session, add):
    add("Лекция об истории Уфы", datetime(2026, 11, 8, 0, 0), source="a")
    event, _ = add("Лекция об истории Уфы", datetime(2026, 11, 8, 18, 0), source="b")
    assert event.start_at == datetime(2026, 11, 8, 18, 0)


def test_timezone_aware_input_is_converted_to_local_time():
    event = EventIn(title="Тест", start_at="2026-11-08T13:30:00Z", source="x")
    assert event.start_at == datetime(2026, 11, 8, 18, 30)
    event = EventIn(title="Тест", start_at="2026-11-08T18:30:00+05:00", source="x")
    assert event.start_at == datetime(2026, 11, 8, 18, 30)
