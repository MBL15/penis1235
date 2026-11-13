"""Время суток, длительность, язык и координаты (ТЗ §8.1, §2): фильтры работают только по тому, что известно."""
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine, text

from app.config import now_local
from app.db import backfill_defaults
from app.models import Base, Event
from app.normalize import canonical_language, derive_duration, detect_language
from app.search import SearchFilters, search
from app.sources.jsonld import record_from_ld

NOW = datetime(2026, 11, 7, 9, 0)
DAY = datetime(2026, 11, 8)


def at(hour, minute=0):
    return DAY.replace(hour=hour, minute=minute)


# ---------- определение ----------

def test_duration_is_derived_only_from_a_known_end_on_the_same_day():
    assert derive_duration(at(19), at(21, 30)) == 150
    assert derive_duration(at(0), at(2)) is None  # время начала не указано (00:00)
    assert derive_duration(at(19), DAY + timedelta(days=1, hours=2)) is None  # окончание на другой день: это не длительность
    assert derive_duration(at(19), at(18)) is None and derive_duration(at(19), None) is None
    assert derive_duration(at(10), at(10, 2)) is None and derive_duration(at(1), at(23)) is None  # нереальные значения


@pytest.mark.parametrize("text, code", [
    ("Спектакль на башкирском языке для детей", "ba"),
    ("Концерт НА ТАТАРСКОМ ЯЗЫКЕ", "tt"),
    ("Лекция на английском языке", "en"),
    ("Показ на русском языке", "ru"),
    ("Башкирский академический театр", None),  # название театра не язык мероприятия
    ("Выступление на башкирском фестивале", None),  # без слова «язык» не считаем
    ("", None),
])
def test_language_is_detected_only_when_named_explicitly(text, code):
    assert detect_language(text) == code


def test_language_values_are_normalized():
    assert [canonical_language(v) for v in ("ba", "башкирский", "на башкирском", "Татарский", "klingon", "", None)] == [
        "ba", "ba", "ba", "tt", None, None, None]


# ---------- приём ----------

def test_ingest_fills_duration_language_and_coordinates(add):
    event, _ = add("Спектакль «Весна»", at(19), end_at=at(21, 15), description="Постановка на башкирском языке.", lat=54.7, lon=56.0)
    assert (event.duration_min, event.language, event.lat, event.lon) == (135, "ba", 54.7, 56.0)
    plain, _ = add("Концерт без данных", at(12), description="Просто концерт.")
    assert (plain.duration_min, plain.language, plain.lat) == (None, None, None)  # ничего не выдумываем
    explicit, _ = add("Лекция", at(15), end_at=at(16), duration_min=45, language="татарский")
    assert (explicit.duration_min, explicit.language) == (45, "tt")  # явные данные источника важнее вывода


def test_a_second_source_fills_the_gaps(add):
    first, _ = add("Балет «Лебединое озеро»", at(18), source="a.example")
    assert first.duration_min is None
    merged, outcome = add("Лебединое озеро. Балет", at(18), source="b.example", end_at=at(20, 30), language="ru")
    assert outcome == "merged" and merged.id == first.id
    assert (merged.duration_min, merged.language) == (150, "ru")


# ---------- поиск ----------

@pytest.fixture
def day(add):
    add("Утренник", at(10), end_at=at(11, 30), category="kids")
    add("Вечерний концерт", at(19), end_at=at(21), category="concert")
    add("Поздний показ", at(21, 30), end_at=at(23, 30), category="cinema")
    add("Выставка без времени", DAY, category="exhibition")  # время не указано (00:00)
    add("Концерт без окончания", at(18, 30), category="concert")
    add("Спектакль на башкирском", at(17), end_at=at(19), description="Спектакль на башкирском языке.", category="theatre")


def titles(session, **kw):
    found, _ = search(session, SearchFilters(date_from=date(2026, 11, 8), date_to=date(2026, 11, 8), **kw), NOW)
    return {e.title for e in found}


def test_start_time_filters_skip_events_whose_time_is_unknown(session, day):
    assert len(titles(session)) == 6
    assert titles(session, time_from=time(18)) == {"Вечерний концерт", "Поздний показ", "Концерт без окончания"}
    assert titles(session, time_from=time(17), time_to=time(19)) == {"Вечерний концерт", "Концерт без окончания", "Спектакль на башкирском"}
    assert titles(session, time_to=time(12)) == {"Утренник"}  # «Выставка без времени» не считается утренней
    assert "Выставка без времени" not in titles(session, time_from=time(0, 1))


def test_ends_by_needs_a_known_duration(session, day):
    assert titles(session, ends_by=time(20)) == {"Утренник", "Спектакль на башкирском"}  # у «Концерт без окончания» длительность неизвестна
    assert titles(session, ends_by=time(21)) == {"Утренник", "Вечерний концерт", "Спектакль на башкирском"}
    assert titles(session, ends_by=time(20), time_from=time(16)) == {"Спектакль на башкирском"}


def test_duration_and_language_filters(session, day):
    assert titles(session, max_duration=120) == {"Утренник", "Вечерний концерт", "Поздний показ", "Спектакль на башкирском"}
    assert titles(session, max_duration=90) == {"Утренник"}
    assert titles(session, language="ba") == {"Спектакль на башкирском"}
    assert titles(session, language="башкирский") == {"Спектакль на башкирском"}
    assert titles(session, language="tt") == set()  # язык указан не у всех: остальные не подходят


def test_filters_are_described_for_the_response():
    described = SearchFilters(time_from=time(19, 5), ends_by=time(22), max_duration=120, language="ba").describe()
    assert described == {"time_from": "19:05", "ends_by": "22:00", "max_duration": 120, "language": "ba"}


# ---------- API ----------

def test_api_accepts_the_new_filters_and_returns_the_new_fields(client, add):
    start = (now_local() + timedelta(days=3)).replace(hour=19, minute=0, second=0, microsecond=0)
    add("Спектакль «Весна»", start, end_at=start + timedelta(hours=2), description="На башкирском языке.", category="theatre")
    add("Концерт утром", start.replace(hour=10), category="concert")
    data = client.get("/api/v1/events", params={"time_from": "18:00", "ends_by": "21:30", "max_duration": 150, "language": "ba"}).json()
    assert data["total"] == 1
    item = data["items"][0]
    assert (item["duration_min"], item["language"], item["language_label"]) == (120, "ba", "башкирский")
    assert client.get("/api/v1/events", params={"time_to": "12:00"}).json()["items"][0]["title"] == "Концерт утром"
    for bad in ({"time_from": "25:00"}, {"time_from": "7:30"}, {"ends_by": "abc"}, {"max_duration": 0}, {"language": "x" * 33}):
        response = client.get("/api/v1/events", params=bad)
        assert response.status_code == 422 and response.json()["error"]["code"] == "validation_error", bad


def test_event_page_shows_duration_and_language(client, add):
    start = (now_local() + timedelta(days=3)).replace(hour=19, minute=0, second=0, microsecond=0)
    event, _ = add("Спектакль «Весна»", start, end_at=start + timedelta(minutes=135), description="Спектакль на башкирском языке.")
    html = client.get(f"/event/{event.id}").text
    assert "Длительность" in html and "около 2 ч 15 мин" in html and "башкирский" in html


def test_moderator_can_correct_duration_and_language(client, admin_key, add):
    start = (now_local() + timedelta(days=3)).replace(hour=19, minute=0, second=0, microsecond=0)
    event, _ = add("Концерт", start)
    url = f"/api/v1/admin/events/{event.id}"
    fixed = client.patch(url, json={"duration_min": 90, "language": "татарский"}, headers=admin_key).json()
    assert (fixed["duration_min"], fixed["language"]) == (90, "tt")
    assert client.patch(url, json={"language": "клингонский"}, headers=admin_key).status_code == 422
    assert client.patch(url, json={"language": None}, headers=admin_key).json()["language"] is None


# ---------- источники и старые данные ----------

def test_schema_org_geo_becomes_coordinates_and_bad_values_are_dropped():
    node = {"@type": "Event", "name": "Концерт", "startDate": "2026-12-01T19:00:00", "url": "https://x.example/e",
            "location": {"@type": "Place", "name": "Зал", "geo": {"latitude": "54.7351", "longitude": 55.9587}}}
    record = record_from_ld(node, source="x.example", today=date(2026, 11, 7))
    assert (record["lat"], record["lon"]) == (54.7351, 55.9587)
    node["location"]["geo"] = {"latitude": 154, "longitude": 55}
    assert "lat" not in record_from_ld(node, source="x.example", today=date(2026, 11, 7))
    node["location"].pop("geo")
    assert "lon" not in record_from_ld(node, source="x.example", today=date(2026, 11, 7))


def test_old_rows_get_duration_and_language_filled_without_touching_updated_at():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for row in (
            (1, "Вечер", "2026-11-08 19:00:00", "2026-11-08 21:00:00", "Обычное описание"),
            (2, "Лекция", "2026-11-08 00:00:00", "2026-11-08 02:00:00", "Спектакль на татарском языке"),
            (3, "Серия", "2026-11-08 19:00:00", "2026-11-20 21:00:00", ""),
        ):
            conn.execute(text(
                "INSERT INTO events (id, title, description, category, city, venue, address, start_at, end_at, age_limit, kids_friendly,"
                " url, image_url, status, sources, dedup_key, search_text, created_at, updated_at, publication, venue_key)"
                " VALUES (:id, :t, :d, 'other', 'Уфа', '', '', :s, :e, 0, 0, '', '', 'scheduled', '[]', '', '',"
                " '2026-01-01 00:00:00', '2026-01-02 00:00:00', 'published', '')"), {"id": row[0], "t": row[1], "s": row[2], "e": row[3], "d": row[4]})
    assert backfill_defaults(engine) == 2  # длительность у первой, язык у второй
    assert backfill_defaults(engine) == 0
    with engine.connect() as conn:
        rows = {r.id: (r.duration_min, r.language, str(r.updated_at)) for r in conn.execute(text("SELECT id, duration_min, language, updated_at FROM events"))}
    assert rows[1][:2] == (120, None) and rows[2][:2] == (None, "tt") and rows[3][:2] == (None, None)  # у «02:00» время начала не указано
    assert all(r[2] == "2026-01-02 00:00:00" for r in rows.values())
    assert Event.__table__.c.duration_min.nullable
