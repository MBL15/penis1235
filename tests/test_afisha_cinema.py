"""Кино на Афише (afisha.ru): список кинотеатров, расписание сеансов, обход по дням. Страницы в tests/fixtures: реальная
структура данных сайта (`window.__nrp ... ['root'] = {...}`), сокращённая до нескольких кинотеатров и двух фильмов."""
from datetime import datetime
from pathlib import Path

import pytest

from app.sources import afisha_cinema
from app.sources.base import BlockedError, SourceError

FIX = Path(__file__).parent / "fixtures"
LIST_HTML = (FIX / "afisha_cinema_list.html").read_text(encoding="utf-8")
SCHEDULE_HTML = (FIX / "afisha_cinema_schedule.html").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 13, 12, 0)


def test_cinema_list_is_parsed_from_the_page_model():
    cinemas = afisha_cinema.parse_cinemas(LIST_HTML)
    assert len(cinemas) == 4
    first = cinemas[0]
    assert first["name"] == "Синема Парк Семья" and first["id"] == 3479
    assert first["schedule_url"] == "/ufa/cinema/sinema-park-semya-3479/movie/"
    assert first["address"].startswith("просп. Октября") and 54 < first["lat"] < 55 and 55 < first["lon"] < 57


def test_page_without_model_is_a_source_error_not_a_crash():
    with pytest.raises(SourceError):
        afisha_cinema.parse_cinemas("<html><body>капча</body></html>")


@pytest.mark.parametrize("text,expected", [("2 часа 20 минут", 140), ("95 минут", 95), ("1 час", 60), ("", None), ("скоро", None), (None, None)])
def test_duration_text(text, expected):
    assert afisha_cinema.duration_minutes(text) == expected


def schedule(now=NOW):
    cinema = afisha_cinema.parse_cinemas(LIST_HTML)[0]
    url = "https://www.afisha.ru/ufa/cinema/sinema-park-semya-3479/movie/"
    return afisha_cinema.parse_schedule(SCHEDULE_HTML, cinema, city="Уфа", page_url=url, now=now)


def test_one_record_per_film_with_facts_only_from_the_page():
    records, more = schedule()
    assert len(records) == 2 and records[0]["title"] == "Рождение империи"
    record = records[0]
    assert record["category"] == "cinema" and record["city"] == "Уфа" and record["venue"] == "Синема Парк Семья"
    assert record["start_at"] == "2026-11-13T23:40:00" and record["price_min"] == record["price_max"] == 520
    assert record["age_limit"] == 12 and record["end_at"] == "2026-11-14T02:00:00"  # 23:40 + 2 ч 20 мин
    assert record["address"].startswith("Уфа, просп. Октября") and record["source"] == "afisha.ru"
    assert "Сеансы 13.11: 23:40" in record["description"] and "Длительность: 2 часа 20 минут" in record["description"]
    assert record["image_url"].startswith("https://") and record["lat"] and record["lon"]
    # вторая страница расписания берётся из ссылок самого сайта
    assert more == ["https://www.afisha.ru/ufa/cinema/sinema-park-semya-3479/movie/?page=2"]


def test_url_is_unique_per_cinema_and_day_so_dedup_does_not_merge_different_screenings():
    records, _ = schedule()
    other_cinema = afisha_cinema.parse_cinemas(LIST_HTML)[1]
    other, _ = afisha_cinema.parse_schedule(SCHEDULE_HTML, other_cinema, city="Уфа", page_url="https://www.afisha.ru/x/", now=NOW)
    assert records[0]["url"] != other[0]["url"]
    assert records[0]["url"].startswith("https://www.afisha.ru/movie/") and "#kino-3479-20261113" in records[0]["url"]


def test_started_sessions_are_not_published():
    records, _ = schedule(now=datetime(2026, 11, 13, 23, 38))
    assert [r["start_at"] for r in records] == ["2026-11-13T23:40:00"]  # сеанс 23:35 уже начался
    late, _ = schedule(now=datetime(2026, 11, 14, 1, 0))
    assert late == []  # все сеансы этого дня позади


class FakeFetcher:
    """Подмена Fetcher: адрес → страница; запоминает, куда ходили."""

    def __init__(self, pages: dict):
        self.pages, self.calls = pages, []

    def get(self, url, **_):
        self.calls.append(url)
        page = self.pages.get(url)
        if isinstance(page, Exception):
            raise page
        if page is None:
            raise SourceError(f"{url}: страницы нет (404)")
        return page


def test_fetch_walks_cities_cinemas_days_and_pages():
    base = "https://www.afisha.ru"
    pages = {f"{base}/ufa/cinema/cinema_list/": LIST_HTML,
             f"{base}/ufa/cinema/sinema-park-semya-3479/movie/": SCHEDULE_HTML,
             f"{base}/ufa/cinema/sinema-park-semya-3479/movie/14-11-2026": SCHEDULE_HTML}
    fetcher = FakeFetcher(pages)
    records = list(afisha_cinema.fetch_records(days=2, fetcher=fetcher, now=NOW))
    assert records and all(r["category"] == "cinema" for r in records)
    assert f"{base}/ufa/cinema/sinema-park-semya-3479/movie/14-11-2026" in fetcher.calls  # второй день
    assert f"{base}/ufa/cinema/sinema-park-semya-3479/movie/?page=2" in fetcher.calls  # страница расписания из пагинации
    # страницы, которых нет (другие кинотеатры в подмене), не обрывают обход
    assert len(fetcher.calls) > 4


def test_missing_city_is_skipped_and_block_stops_the_walk():
    fetcher = FakeFetcher({})
    assert list(afisha_cinema.fetch_records(cities={"nowhere": "Нигде"}, fetcher=fetcher, now=NOW)) == []
    blocked = FakeFetcher({"https://www.afisha.ru/ufa/cinema/cinema_list/": BlockedError("429")})
    with pytest.raises(BlockedError):
        list(afisha_cinema.fetch_records(fetcher=blocked, now=NOW))


def test_source_is_registered_in_sync_and_scheduler():
    from app.scheduler import ScheduleError  # noqa: F401  (модуль импортируется без ошибок)
    from app.sync import SOURCES, producers
    from argparse import Namespace

    assert "afisha-cinema" in SOURCES
    args = Namespace(pages=1, limit=1, cities="ufa,sterlitamak,salavat", delay=0.0, days=2)
    assert "afisha-cinema" in producers(args)
