"""Справочники городов, категорий и площадок и фильтр по площадке (ТЗ §4, §6.3)."""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.config import now_local
from app.search import SearchFilters, search, venues

NOW = now_local()

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def soon(days, hour=19):
    """Будущая дата относительно настоящего времени: справочники считают «актуальное» по часам сервера."""
    return (now_local() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


@pytest.fixture
def places(add):
    add("Концерт А", soon(2), venue="Дворец молодёжи", city="Уфа", category="concert")
    add("Концерт Б", soon(3), venue="Дворец Молодежи", city="Уфа", category="concert")
    add("Спектакль В", soon(3, 18), venue="Дворец Молодежи", city="Уфа", category="theatre")
    add("Балет Г", soon(4), venue="Театр оперы и балета", city="Уфа", category="theatre")
    add("Драма Д", soon(4), venue="Русский драматический театр", city="Стерлитамак", category="theatre")
    add("Без площадки", soon(5), venue="", city="Уфа")
    add("Прошедшее", datetime(2026, 10, 1, 19, 0), venue="Старый зал", city="Уфа")
    add("Отменённое", soon(6), venue="Отменный зал", city="Уфа", status="cancelled")


def test_spellings_of_one_venue_are_grouped(session, places):
    found = venues(session, NOW)
    top = found[0]
    assert top["name"] == "Дворец Молодежи" and top["count"] == 3 and top["city"] == "Уфа"
    assert top["names"] == ["Дворец молодёжи"]  # другое написание той же площадки
    names = [v["name"] for v in found]
    assert "Старый зал" not in names and "Отменный зал" not in names and "" not in names  # прошедшие, отменённые и пустые не нужны
    assert names.index("Дворец Молодежи") == 0


def test_venue_directory_can_be_narrowed_by_city_and_text(session, places):
    assert [v["name"] for v in venues(session, NOW, city="Стерлитамак")] == ["Русский драматический театр"]
    assert [v["name"] for v in venues(session, NOW, q="ОПЕРЫ")] == ["Театр оперы и балета"]
    assert venues(session, NOW, q="несуществующая") == []
    assert len(venues(session, NOW, limit=1)) == 1


def titles(session, **kw):
    found, _ = search(session, SearchFilters(**kw), NOW)
    return {e.title for e in found}


def test_venue_filter_ignores_case_yo_and_punctuation(session, places):
    assert titles(session, venue="дворец молодежи") == {"Концерт А", "Концерт Б", "Спектакль В"}
    assert titles(session, venue="ДВОРЕЦ МОЛОДЁЖИ") == {"Концерт А", "Концерт Б", "Спектакль В"}
    assert titles(session, venue="оперы и балета") == {"Балет Г"}
    assert titles(session, venue="дворец", category="theatre") == {"Спектакль В"}  # сочетается с другими фильтрами
    assert titles(session, venue="«!!!»") != set()  # одни знаки препинания не должны превращаться в «ничего не найдено»


def test_directory_endpoints(client, places):
    cities = client.get("/api/v1/cities").json()
    assert {"name": "Стерлитамак", "count": 1} in cities and cities[0]["name"] == "Уфа"
    categories = {c["slug"]: c for c in client.get("/api/v1/categories").json()}
    assert categories["theatre"]["count"] == 3 and categories["theatre"]["label"] == "Театр" and categories["festival"]["count"] == 0
    listing = client.get("/api/v1/venues")
    assert listing.status_code == 200 and listing.headers["etag"] and listing.headers["cache-control"] == "public, max-age=300"
    assert listing.json()[0]["names"] == ["Дворец молодёжи"]
    assert [v["name"] for v in client.get("/api/v1/venues", params={"city": "Стерлитамак"}).json()] == ["Русский драматический театр"]
    assert client.get("/api/v1/venues", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/venues", headers={"If-None-Match": listing.headers["etag"]}).status_code == 304


def test_events_endpoint_filters_by_venue(client, places):
    data = client.get("/api/v1/events", params={"venue": "дворец молодежи", "include_past": True}).json()
    assert data["total"] == 3 and {e["venue"] for e in data["items"]} == {"Дворец молодёжи", "Дворец Молодежи"}
    assert client.get("/api/v1/events", params={"venue": "x" * 201}).status_code == 422
    assert "venue" in {p["name"] for p in client.get("/openapi.json").json()["paths"]["/api/v1/events"]["get"]["parameters"]}


def test_site_has_a_venue_field_wired_to_the_url_and_reset():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="f-venue"' in html and 'list="venue-options"' in html and 'id="venue-options"' in html
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    for needle in ("set('venue'", "p.get('venue')", "'f-venue'", "/api/v1/venues"):
        assert needle in script, needle
