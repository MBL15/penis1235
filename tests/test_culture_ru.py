import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Event
from app.sources import culture_ru
from app.sources.culture_ru import SourceError, fetch_records, parse_page, to_record
from app.sync import sync_records

# Реальные карточки со страницы https://www.culture.ru/afisha/respublika-bashkortostan (урезаны до нужных полей).
PAGE = (Path(__file__).parent / "fixtures" / "culture_ru_page.html").read_text(encoding="utf-8")
TODAY = date(2026, 11, 7)
ROBOTS_OK = "User-agent: *\nDisallow: /?\nDisallow: /search\nDisallow: /*city=*\n"


def records():
    return [to_record(item, TODAY) for item in parse_page(PAGE).items]


def by_title(title_part):
    return next(r for r in records() if title_part in r["title"])


def test_parse_page_reads_cards_and_page_count():
    parsed = parse_page(PAGE)
    assert len(parsed.items) == 5 and parsed.total_pages == 413


def test_broken_markup_is_reported_not_swallowed():
    with pytest.raises(SourceError):
        parse_page("<html>без данных</html>")
    with pytest.raises(SourceError):
        parse_page('<script id="__NEXT_DATA__" type="application/json">{"props": {}}</script>')


def test_single_event_converts_utc_to_ufa_time():
    r = by_title("В ритме вдохновения")
    assert r["start_at"] == "2026-11-12T18:30:00"  # 13:30 UTC → 18:30 по Уфе
    assert r["end_at"] == "2026-11-12T19:45:00"
    assert (r["city"], r["category"], r["age_limit"]) == ("Уфа", "concert", 6)
    assert (r["price_min"], r["price_max"]) == (300, 300)
    assert r["venue"].startswith("Учебный театр") and "Цюрупы" in r["address"]
    assert r["url"] == "https://www.culture.ru/events/7298249_7298249/koncert-v-ritme-vdokhnoveniya"
    assert r["image_url"].startswith("https://cdn.culture.ru/images/")
    assert r["source"] == "culture.ru" and r["source_url"] == r["url"]


def test_series_from_a_date_gets_a_date_range_and_lecture_category():
    r = by_title("Новогодняя мозаика")
    assert r["start_at"] == "2026-12-06T13:00:00" and r["end_at"] == "2026-12-27T14:00:00"
    assert r["category"] == "lecture" and r["price_min"] == 2000


def test_permanent_exhibition_starts_today_and_ends_on_given_date():
    r = by_title("Золотая Орда")
    assert r["start_at"] == "2026-11-07T00:00:00" and r["end_at"] == "2027-12-30T18:00:00"
    assert r["category"] == "exhibition" and (r["price_min"], r["price_max"]) == (100, 450)


def test_free_event_keeps_zero_prices():
    r = by_title("Камерная музыка")
    assert (r["price_min"], r["price_max"]) == (0, 0)


def test_missing_optional_fields_do_not_break_conversion():
    item = parse_page(PAGE).items[0]
    for key in ("price", "thumbnailFile", "selectedLocalePlace", "nearestSeancePlace", "places", "genres", "ageRestriction"):
        item.pop(key, None)
    r = to_record(item, TODAY)
    assert "price_min" not in r and r["image_url"] == "" and r["category"] == "concert"  # «Концерт» из названия


def test_sync_creates_then_is_idempotent(session):
    first = sync_records(session, records())
    assert first == {"created": 5, "merged": 0, "unchanged": 0, "skipped": 0, "cross_source": 0}
    second = sync_records(session, records())
    assert second["created"] == 0 and second["merged"] == 0 and second["unchanged"] == 5
    event = session.scalar(select(Event).where(Event.title.like("%Золотая Орда%")))
    assert event.sources == [{"name": "culture.ru", "url": event.url, "status": "scheduled"}] and event.end_at.year == 2027


def test_sync_skips_bad_records_but_keeps_going(session):
    good = records()[0]
    stats = sync_records(session, [{"title": "x"}, good])
    assert stats["created"] == 1 and stats["skipped"] == 1


class FakeSite:
    def __init__(self, robots=ROBOTS_OK):
        self.robots, self.requested = robots, []

    def __call__(self, url):
        self.requested.append(url)
        return self.robots if url.endswith("/robots.txt") else PAGE


def test_crawl_is_polite_and_bounded():
    site, pauses = FakeSite(), []
    out = list(fetch_records(max_pages=3, delay=1.5, fetch=site, sleep=pauses.append, today=TODAY))
    assert len(out) == 15  # 3 страницы по 5 карточек в образце
    assert [u.rsplit("?", 1)[-1] for u in site.requested if "page=" in u] == ["page=1", "page=2", "page=3"]
    assert pauses == [1.5, 1.5]  # пауза между страницами, но не после последней


def test_one_malformed_card_is_skipped_not_fatal():
    data = json.loads(culture_ru._NEXT_DATA.search(PAGE).group(1))
    del data["props"]["pageProps"]["events"]["items"][0]["cardSchedule"]
    broken = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'

    def fetch(url):
        return ROBOTS_OK if url.endswith("/robots.txt") else broken

    out = list(fetch_records(max_pages=1, fetch=fetch, sleep=lambda s: None, today=TODAY))
    assert len(out) == 4  # из пяти карточек пропущена одна битая


def test_crawl_stops_when_robots_forbid_the_listing():
    site = FakeSite(robots="User-agent: *\nDisallow: /afisha\n")
    with pytest.raises(SourceError, match="robots.txt"):
        list(fetch_records(max_pages=1, fetch=site, sleep=lambda s: None))
    assert not any("page=" in u for u in site.requested)  # страницы афиши даже не запрашивались


def test_crawl_stops_when_robots_cannot_be_read():
    def down(url):
        raise SourceError("timeout")

    with pytest.raises(SourceError):
        list(fetch_records(max_pages=1, fetch=down))


def test_user_agent_identifies_the_bot():
    assert culture_ru.USER_AGENT.startswith("bashafisha-hackathon/")
