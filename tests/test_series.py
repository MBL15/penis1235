"""Серии показов (ТЗ §9.2 №3): театр или концерт, записанный источником как период «первая – последняя дата»,
не должен находиться по любому дню периода. Известен только один показ."""
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db import add_missing_columns
from app.formatting import is_past, when_text
from app.maintenance import fix_series
from app.models import Base, Event
from app.schemas import EventIn
from app.search import SearchFilters, search
from app.sources import kassir
from app.sources.jsonld import record_from_ld

from .conftest import NOW  # суббота 7 ноября 2026, 15:00

TODAY = date(2026, 11, 7)


def node(name, start, end, url="https://ufa.kassir.ru/teatr/x", **extra):
    return {"@type": "Event", "name": name, "startDate": start, "endDate": end, "url": url,
            "location": {"@type": "Place", "name": "Театр", "address": "Уфа, ул Ленина, 1"}, **extra}


def titles(session, **kw):
    found, _ = search(session, SearchFilters(**kw), NOW)
    return {e.title for e in found}


# ---------- разбор источника ----------

def test_period_of_a_performance_becomes_a_series_and_keeps_the_known_session():
    record = record_from_ld(node("Кровь хищника", "2026-11-13T19:00:00+00:00", "2026-12-04T20:00:00+00:00"),
                            source="kassir.ru", tz_mode="local", today=TODAY, default_category="theatre", period_as_series=True)
    assert record["start_at"] == "2026-11-13T19:00:00"
    assert record["series_until"] == "2026-12-04T20:00:00" and "end_at" not in record


def test_series_that_already_started_keeps_its_real_first_date_instead_of_today():
    record = record_from_ld(node("Кровь хищника", "2026-11-01T19:00:00+00:00", "2026-12-04T20:00:00+00:00"),
                            source="kassir.ru", tz_mode="local", today=TODAY, default_category="theatre", period_as_series=True)
    assert record["start_at"] == "2026-11-01T19:00:00"  # раньше start подменялся на сегодня, и «сегодня» находило серию


@pytest.mark.parametrize("category", ["exhibition", "excursion", "festival"])
def test_continuous_categories_keep_their_period(category):
    record = record_from_ld(node("Выставка", "2026-11-01T00:00:00+00:00", "2026-12-04T00:00:00+00:00"),
                            source="kassir.ru", tz_mode="local", today=TODAY, default_category=category, period_as_series=True)
    assert record["end_at"] == "2026-12-04T00:00:00" and "series_until" not in record
    assert record["start_at"] == "2026-11-07T00:00:00"  # идущая выставка начинается «сегодня», как и раньше


def test_other_sources_are_not_affected_unless_they_ask_for_it():
    record = record_from_ld(node("Спектакль", "2026-11-13T19:00:00+00:00", "2026-12-04T20:00:00+00:00"),
                            source="sites", tz_mode="local", today=TODAY, default_category="theatre")
    assert record["end_at"] == "2026-12-04T20:00:00" and "series_until" not in record


def test_kassir_parser_applies_series_rule_and_path_category():
    html = ('<script type="application/ld+json">' + __import__("json").dumps(
        {"@graph": [node("Кровь хищника", "2026-11-13T19:00:00+00:00", "2026-12-04T20:00:00+00:00")]}) + "</script>")
    (record,) = kassir.parse_section(html, city="Уфа", section_url="https://ufa.kassir.ru/", today=TODAY)
    assert record["category"] == "theatre" and record["series_until"].startswith("2026-12-04")


def test_series_is_dropped_when_it_ends_on_the_day_of_the_session_or_the_event_has_a_period():
    base = {"title": "Спектакль", "start_at": datetime(2026, 11, 13, 19, 0), "source": "t"}
    assert EventIn(**base, series_until=datetime(2026, 11, 13, 21, 0)).series_until is None
    assert EventIn(**base, series_until=datetime(2026, 12, 4), end_at=datetime(2026, 12, 4)).series_until is None
    assert EventIn(**base, series_until=datetime(2026, 12, 4)).series_until == datetime(2026, 12, 4)


# ---------- поиск и показ ----------

@pytest.fixture
def series(add):
    add("Серия в пятницу", datetime(2026, 11, 6, 19, 0), series_until=datetime(2026, 12, 4, 20, 0), category="theatre")
    add("Серия со следующей пятницы", datetime(2026, 11, 13, 19, 0), series_until=datetime(2026, 12, 4, 20, 0), category="theatre")
    add("Разовый концерт", datetime(2026, 11, 8, 19, 0), category="concert")
    add("Серия закончилась", datetime(2026, 10, 1, 19, 0), series_until=datetime(2026, 11, 3, 20, 0), category="theatre")


def test_running_series_is_listed_by_default_but_not_on_days_without_a_known_session(session, series):
    assert {"Серия в пятницу", "Серия со следующей пятницы"} <= titles(session)
    assert "Серия закончилась" not in titles(session)
    sunday = titles(session, date_from=date(2026, 11, 8), date_to=date(2026, 11, 8))
    assert sunday == {"Разовый концерт"}  # раньше серия «шла» каждый день периода и попадала на воскресенье
    assert "Серия в пятницу" not in titles(session, date_from=date(2026, 11, 7), date_to=date(2026, 11, 7))


def test_series_is_found_on_the_day_of_its_known_session(session, series):
    friday = titles(session, date_from=date(2026, 11, 13), date_to=date(2026, 11, 13))
    assert friday == {"Серия со следующей пятницы"}


def test_series_goes_after_one_off_events_when_already_running(session, series):
    found, _ = search(session, SearchFilters(limit=10), NOW)
    order = [e.title for e in found]
    assert order.index("Разовый концерт") < order.index("Серия в пятницу")


def test_series_text_does_not_invent_dates(session, series):
    running, upcoming = (session.query(Event).filter_by(title=t).one() for t in ("Серия в пятницу", "Серия со следующей пятницы"))
    assert when_text(running, NOW) == "показы до 4 декабря"
    assert when_text(upcoming, NOW) == "пт, 13 ноября, 19:00 · показы до 4 декабря"
    assert not is_past(running, NOW)
    assert is_past(running, datetime(2026, 12, 5))


def test_api_returns_series_until(client, series):
    items = client.get("/api/events", params={"q": "серия", "limit": 5}).json()["items"]
    assert {i["title"]: i["series_until"] for i in items}["Серия в пятницу"].startswith("2026-12-04")


# ---------- приём и исправление старых данных ----------

def test_resync_turns_an_old_period_into_a_series(session, add):
    old, _ = add("Кровь хищника", datetime(2026, 11, 13, 19, 0), source="kassir.ru", category="theatre",
                 end_at=datetime(2026, 12, 4, 20, 0), url="https://ufa.kassir.ru/teatr/x")
    assert old.end_at is not None
    again, outcome = add("Кровь хищника", datetime(2026, 11, 13, 19, 0), source="kassir.ru", category="theatre",
                         series_until=datetime(2026, 12, 4, 20, 0), url="https://ufa.kassir.ru/teatr/x")
    assert outcome == "merged" and again.end_at is None and again.series_until == datetime(2026, 12, 4, 20, 0)


def test_fix_series_changes_only_kassir_performances(session, add):
    kassir_show, _ = add("Балет", datetime(2026, 12, 3, 19, 0), source="kassir.ru", category="theatre", end_at=datetime(2027, 1, 23, 20, 0))
    expo, _ = add("Выставка", datetime(2026, 11, 7), source="kassir.ru", category="exhibition", end_at=datetime(2027, 2, 23))
    yandex, _ = add("Экскурсия-спектакль", datetime(2026, 11, 9, 19, 0), source="afisha.yandex.ru", category="theatre", end_at=datetime(2026, 12, 9))
    assert len(fix_series(session)) == 1 and kassir_show.end_at is not None  # без apply ничего не меняется
    assert [e.title for e in fix_series(session, apply=True)] == ["Балет"]
    assert kassir_show.end_at is None and kassir_show.series_until == datetime(2027, 1, 23, 20, 0)
    assert expo.end_at is not None and yandex.end_at is not None
    assert fix_series(session, apply=True) == []  # повторный запуск безопасен


def test_old_database_gets_the_new_column_without_losing_rows():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE events (id INTEGER PRIMARY KEY, title VARCHAR(300))"))
        conn.execute(text("INSERT INTO events (id, title) VALUES (1, 'старая запись')"))
    added = add_missing_columns(engine)
    assert "events.series_until" in added
    assert "series_until" in {c["name"] for c in inspect(engine).get_columns("events")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT title FROM events")).scalar() == "старая запись"
    assert add_missing_columns(engine) == []  # повторный запуск ничего не делает
    assert Base.metadata.tables["events"].columns["series_until"].nullable
