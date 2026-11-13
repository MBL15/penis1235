"""Новые источники: общая основа (robots, блокировки), schema.org, Яндекс Афиша, Kassir.ru, KudaGo, сайты из файла."""
import json
import urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.sources import kassir, kudago, sites, yandex_afisha
from app.sources.base import BlockedError, Fetcher, SourceError, parse_datetime
from app.sources.jsonld import extract_events, extract_ld, record_from_ld

TODAY = date(2026, 11, 13)


# ---------- подмена сети ----------

class FakeResponse:
    def __init__(self, body: str, url: str, status: int = 200):
        self.body, self.url, self.status = body.encode(), url, status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


class FakeOpener:
    """pages: адрес → тело | число (код ошибки) | (тело, итоговый адрес). Запоминает порядок запросов."""

    def __init__(self, pages: dict):
        self.pages, self.calls = pages, []

    def open(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        page = self.pages.get(url, 404)
        if isinstance(page, int):
            raise urllib.error.HTTPError(url, page, "ошибка", {}, None)
        if isinstance(page, tuple):
            return FakeResponse(page[0], page[1])
        return FakeResponse(page, url)


def fetcher(pages, hosts=("example.ru",), **kw):
    clock = {"t": 0.0}
    waits: list[float] = []

    def sleep(seconds):
        waits.append(seconds)
        clock["t"] += seconds

    f = Fetcher(hosts, opener=FakeOpener(pages), sleep=sleep, clock=lambda: clock["t"], **kw)
    f.waits = waits
    return f


# ---------- Fetcher ----------

def test_robots_disallow_stops_the_page_before_it_is_requested():
    f = fetcher({"https://example.ru/robots.txt": "User-agent: *\nDisallow: /private\n", "https://example.ru/ok": "ok", "https://example.ru/private/x": "секрет"})
    assert f.get("https://example.ru/ok") == "ok"
    with pytest.raises(SourceError, match="robots.txt запрещает"):
        f.get("https://example.ru/private/x")
    assert "https://example.ru/private/x" not in f.opener.calls


def test_missing_robots_means_no_limits_but_unreadable_robots_stops():
    assert fetcher({"https://example.ru/robots.txt": 404, "https://example.ru/a": "тело"}).get("https://example.ru/a") == "тело"
    with pytest.raises(BlockedError):  # robots.txt закрыт (403): сайт просит нас не ходить
        fetcher({"https://example.ru/robots.txt": 403}).get("https://example.ru/a")


def test_hosts_https_and_redirects_are_checked():
    f = fetcher({"https://example.ru/robots.txt": 404, "https://example.ru/go": ("перенаправили", "https://evil.example/x")})
    with pytest.raises(SourceError, match="вне списка"):
        f.get("https://other.ru/a")
    with pytest.raises(SourceError, match="вне списка"):
        f.get("http://example.ru/a")  # не HTTPS
    with pytest.raises(SourceError, match="посторонний адрес"):
        f.get("https://example.ru/go")
    assert f.host_ok("https://ufa.example.ru/a") and not f.host_ok("https://notexample.ru/a")


@pytest.mark.parametrize("code", [403, 429])
def test_block_responses_end_the_crawl(code):
    f = fetcher({"https://example.ru/robots.txt": 404, "https://example.ru/a": code})
    with pytest.raises(BlockedError):
        f.get("https://example.ru/a")
    assert f.opener.calls.count("https://example.ru/a") == 1  # без повторов: просят остановиться


def test_captcha_page_is_not_bypassed():
    f = fetcher({"https://example.ru/robots.txt": 404, "https://example.ru/a": ("<html>smartcaptcha</html>", "https://example.ru/showcaptcha?x=1")})
    with pytest.raises(BlockedError, match="капч"):
        f.get("https://example.ru/a")
    big = "<script>var captcha=1</script>" + "x" * 40_000  # слово «captcha» в скриптах большой страницы капчей не считается
    assert len(fetcher({"https://example.ru/robots.txt": 404, "https://example.ru/b": big}).get("https://example.ru/b")) > 40_000


def test_pause_between_requests_to_one_host_and_size_limit():
    f = fetcher({"https://example.ru/robots.txt": 404, "https://example.ru/1": "a", "https://example.ru/2": "b"}, delay=2.0)
    f.get("https://example.ru/1")
    f.get("https://example.ru/2")
    assert any(w >= 1.9 for w in f.waits)
    with pytest.raises(SourceError, match="больше"):
        fetcher({"https://example.ru/robots.txt": 404, "https://example.ru/big": "x" * 50}, max_bytes=10).get("https://example.ru/big")


def test_parse_datetime_modes():
    assert parse_datetime("2026-11-12T21:00:00.000Z") == datetime(2026, 11, 13, 2, 0)  # честный UTC → Уфа
    assert parse_datetime("2026-12-13T13:00:00+00:00", tz_mode="local") == datetime(2026, 12, 13, 13, 0)  # метка UTC ложная
    assert parse_datetime("2026-12-13T13:00:00") == datetime(2026, 12, 13, 13, 0)  # без смещения время уже местное
    assert parse_datetime("не дата") is None and parse_datetime(None) is None


# ---------- schema.org ----------

def ld_page(*nodes, graph=False):
    payload = {"@context": "https://schema.org", "@graph": list(nodes)} if graph else list(nodes) if len(nodes) > 1 else nodes[0]
    return f'<html><script type="application/ld+json">{json.dumps(payload, ensure_ascii=False)}</script></html>'


EVENT = {
    "@type": "MusicEvent", "name": "Концерт &laquo;Осень&raquo;", "description": "<p>Живой оркестр</p>", "startDate": "2026-11-20T19:00:00+05:00",
    "url": "https://example.ru/e/1", "image": {"@type": "ImageObject", "contentUrl": "https://cdn.example.ru/1.jpg"},
    "location": {"@type": "Place", "name": "Филармония", "address": {"@type": "PostalAddress", "streetAddress": "ул. Гоголя, 58", "addressLocality": "Уфа"}},
    "offers": [{"@type": "Offer", "price": "500.00"}, {"@type": "Offer", "price": 1500}],
}


def test_extract_ld_flattens_graphs_lists_and_ignores_broken_json():
    html = ld_page({"@type": "WebSite"}, EVENT, graph=True) + '<script type="application/ld+json">{сломано</script>' + ld_page(EVENT, {"@type": "Organization"})
    assert len(extract_events(html)) == 2
    assert [n.get("@type") for n in extract_ld(html)].count("WebSite") == 1


def test_event_record_fields():
    record = record_from_ld(EVENT, source="example.ru", today=TODAY)
    assert record["title"] == "Концерт «Осень»" and record["description"] == "Живой оркестр"  # сущности и теги убраны
    assert record["start_at"] == "2026-11-20T19:00:00" and record["category"] == "concert"
    assert (record["venue"], record["address"], record["city"]) == ("Филармония", "ул. Гоголя, 58", "Уфа")
    assert (record["price_min"], record["price_max"]) == (500, 1500)
    assert record["image_url"] == "https://cdn.example.ru/1.jpg" and record["source"] == "example.ru"


@pytest.mark.parametrize("status, expected", [("https://schema.org/EventCancelled", "cancelled"), ("EventPostponed", "rescheduled"),
                                              ("https://schema.org/EventRescheduled", "rescheduled"), ("https://schema.org/EventScheduled", "scheduled")])
def test_event_status_mapping(status, expected):
    assert record_from_ld({**EVENT, "eventStatus": status}, source="s", today=TODAY)["status"] == expected


def test_sold_out_offers_and_prices():
    sold = {**EVENT, "offers": {"@type": "AggregateOffer", "lowPrice": "300", "highPrice": "900", "availability": "https://schema.org/SoldOut"}}
    record = record_from_ld(sold, source="s", today=TODAY)
    assert record["status"] == "sold_out" and (record["price_min"], record["price_max"]) == (300, 900)
    cancelled = {**sold, "eventStatus": "EventCancelled"}
    assert record_from_ld(cancelled, source="s", today=TODAY)["status"] == "cancelled"  # отмена важнее «распродано»
    free = record_from_ld({**EVENT, "offers": {"price": 0}}, source="s", today=TODAY)
    assert free["price_min"] == 0


def test_past_events_skipped_and_running_exhibition_starts_today():
    assert record_from_ld({**EVENT, "startDate": "2026-11-01T19:00:00"}, source="s", today=TODAY) is None
    running = record_from_ld({**EVENT, "startDate": "2026-09-01T00:00:00", "endDate": "2026-12-31T00:00:00"}, source="s", today=TODAY)
    assert running["start_at"] == "2026-11-13T00:00:00" and running["end_at"] == "2026-12-31T00:00:00"
    assert record_from_ld({"@type": "Event", "name": "x"}, source="s", today=TODAY) is None  # нет даты


def test_description_equal_to_title_is_dropped_and_city_comes_from_address_text():
    node = {"@type": "Event", "name": "Шоу", "description": "Шоу", "startDate": "2026-11-25T18:00:00", "location": {"name": "Арена", "address": "Стерлитамак, ул Мира, 1"}}
    record = record_from_ld(node, source="s", today=TODAY)
    assert "description" not in record and record["city"] == "Стерлитамак"


# ---------- Яндекс Афиша ----------

def yandex_page(*, dates, times, permanent=False, ended=None, rating="12+", rubric="concert", ld_extra=None, title="Большой концерт"):
    """Страница события в том виде, как её отдаёт Яндекс Афиша: JSON-LD (с датами периода в UTC) + Apollo-состояние."""
    ld = {"@context": "https://schema.org", "@type": "MusicEvent", "name": title, "description": "Описание из источника", "url": "https://afisha.yandex.ru/ufa/concert/bolshoi",
          "startDate": "2026-11-12T21:00:00.000Z", "endDate": "2026-11-12T21:00:00.000Z", "eventStatus": "https://schema.org/EventScheduled",
          "location": {"@type": "Place", "name": "ГКЗ «Башкортостан»", "address": {"@type": "PostalAddress", "streetAddress": "ул. Ленина, 50", "addressLocality": "Уфа"}},
          "offers": {"@type": "AggregateOffer", "lowPrice": "2000.00", "highPrice": "5600.00", "priceCurrency": "RUB", "availability": "https://schema.org/InStock"}, **(ld_extra or {})}
    sessions = [{"session": {"datetime": f"{d}T{t}"}} for d, t in times.items()]
    state = {
        "ROOT_QUERY": {
            'eventScheduleInfo({"id":"1"})': {"dates": dates, "permanent": permanent, "dateStarted": dates[0] if dates else None, "dateEnd": ended or (dates[-1] if dates else None),
                                              "regularity": {"singleShowtime": None, "isRegular": False}},
            'eventScheduleOther({"id":"1"})': {"byDate": [{"date": d, "sessions": [s]} for d, s in zip(times, sessions)]},
        },
        "Event:1": {"__typename": "Event", "id": "1", "title": title, "contentRating": rating, "type": {"code": rubric}},
    }
    return (f'<html><script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>'
            f"<script>window['__APOLLO_STATE__'] = {json.dumps(state, ensure_ascii=False)}</script></html>")


URL = "https://afisha.yandex.ru/ufa/concert/bolshoi"


def test_yandex_takes_session_time_from_the_schedule_not_from_json_ld():
    html = yandex_page(dates=["2026-11-13"], times={"2026-11-13": "19:00:00"})
    [record] = yandex_afisha.parse_event_page(html, URL, city="Уфа", today=TODAY)
    assert record["start_at"] == "2026-11-13T19:00:00"  # в JSON-LD «2026-11-12T21:00Z» (период, а не сеанс)
    assert (record["price_min"], record["price_max"], record["age_limit"], record["category"]) == (2000, 5600, 12, "concert")
    assert (record["venue"], record["address"], record["city"], record["source"]) == ("ГКЗ «Башкортостан»", "ул. Ленина, 50", "Уфа", "afisha.yandex.ru")
    assert record["url"] == URL  # одна дата: адрес без #


def test_yandex_gives_every_date_its_own_url_and_reuses_a_common_time():
    html = yandex_page(dates=["2026-11-14", "2026-12-20", "2026-11-01"], times={"2026-11-14": "18:00:00"})
    records = yandex_afisha.parse_event_page(html, URL, city="Уфа", today=TODAY)
    assert [r["start_at"] for r in records] == ["2026-11-14T18:00:00", "2026-12-20T18:00:00"]  # прошедшая дата отброшена, время одно у сеансов
    assert [r["url"] for r in records] == [f"{URL}#2026-11-14", f"{URL}#2026-12-20"]


def test_yandex_time_unknown_when_sessions_differ():
    html = yandex_page(dates=["2026-11-14", "2026-11-15", "2026-11-16"], times={"2026-11-14": "12:00:00", "2026-11-15": "18:00:00"})
    starts = [r["start_at"] for r in yandex_afisha.parse_event_page(html, URL, city="Уфа", today=TODAY)]
    assert starts == ["2026-11-14T12:00:00", "2026-11-15T18:00:00", "2026-11-16T00:00:00"]  # без выдумок: у третьей даты только день


def test_yandex_permanent_event_becomes_one_record_with_a_period():
    dates = [date(2026, 11, 13).fromordinal(date(2026, 11, 13).toordinal() + i).isoformat() for i in range(40)]
    html = yandex_page(dates=dates, times={}, permanent=True, ended="2026-12-22", rubric="art", title="Парк альпак")
    [record] = yandex_afisha.parse_event_page(html, URL, city="Уфа", today=TODAY)
    assert record["start_at"] == "2026-11-13T00:00:00" and record["end_at"].startswith("2026-12-22") and record["category"] == "exhibition"
    assert "#" in record["url"]


def test_yandex_page_without_data_or_with_changed_markup_raises_clearly():
    with pytest.raises(SourceError, match="APOLLO"):
        yandex_afisha.parse_event_page("<html>ничего</html>", URL, city="Уфа", today=TODAY)
    no_event = "<html><script>window['__APOLLO_STATE__'] = {\"ROOT_QUERY\": {}}</script></html>"
    with pytest.raises(SourceError, match="нет события"):
        yandex_afisha.parse_event_page(no_event, URL, city="Уфа", today=TODAY)


SITEMAP = """<urlset>
<url><loc>https://afisha.yandex.ru/ufa/concert/bolshoi</loc><lastmod>2026-11-12T10:00:00Z</lastmod></url>
<url><loc>https://afisha.yandex.ru/ufa/concert/nov-15</loc><lastmod>2026-11-12T10:00:00Z</lastmod></url>
<url><loc>https://afisha.yandex.ru/ufa/selections/all-events-concert</loc></url>
<url><loc>https://afisha.yandex.ru/ufa/cinema/kino-film</loc></url>
<url><loc>https://afisha.yandex.ru/ufa/concert/places/zal</loc></url>
<url><loc>https://afisha.yandex.ru/moscow/concert/chuzhoi-gorod</loc></url>
<url><loc>https://afisha.yandex.ru/sterlitamak/theatre_show/spektakl</loc><lastmod>2026-11-10T10:00:00Z</lastmod></url>
</urlset>"""


def test_yandex_sitemap_keeps_only_event_pages_of_chosen_cities():
    found = yandex_afisha.event_urls_from_sitemap(SITEMAP, {"ufa": "Уфа", "sterlitamak": "Стерлитамак"})
    assert sorted(found) == ["https://afisha.yandex.ru/sterlitamak/theatre_show/spektakl", "https://afisha.yandex.ru/ufa/concert/bolshoi"]
    assert list(yandex_afisha.event_urls_from_sitemap(SITEMAP, {"ufa": "Уфа"})) == ["https://afisha.yandex.ru/ufa/concert/bolshoi"]


def test_yandex_crawl_uses_cache_visits_fresh_pages_first_and_stops_on_block(tmp_path):
    pages = {"https://afisha.yandex.ru/robots.txt": "User-agent: *\nDisallow: /api\n", "https://afisha.yandex.ru/sitemap.xml": "<loc>https://afisha.yandex.ru/sitemap/0.xml</loc>",
             "https://afisha.yandex.ru/sitemap/0.xml": SITEMAP.replace("</urlset>", '<url><loc>https://afisha.yandex.ru/ufa/standup/shou</loc><lastmod>2026-11-13T10:00:00Z</lastmod></url></urlset>'),
             URL: yandex_page(dates=["2026-11-20"], times={"2026-11-20": "19:00:00"}),
             "https://afisha.yandex.ru/ufa/standup/shou": yandex_page(dates=["2026-11-21"], times={"2026-11-21": "20:00:00"}, rubric="standup", title="Шоу")}
    f = fetcher(pages, hosts=yandex_afisha.HOSTS, delay=0)
    cache = tmp_path / "urls.json"
    records = list(yandex_afisha.fetch_records(5, fetcher=f, today=TODAY, cache=cache))
    assert [r["title"] for r in records] == ["Шоу", "Большой концерт"]  # недавно обновлённая страница первой
    assert cache.exists()
    again = fetcher({**pages, "https://afisha.yandex.ru/sitemap.xml": 500}, hosts=yandex_afisha.HOSTS, delay=0)  # sitemap не нужен: кэш свежий
    assert len(list(yandex_afisha.fetch_records(5, fetcher=again, today=TODAY, cache=cache))) == 2
    blocked = fetcher({**pages, URL: 429}, hosts=yandex_afisha.HOSTS, delay=0)
    with pytest.raises(BlockedError):
        list(yandex_afisha.fetch_records(5, fetcher=blocked, today=TODAY, cache=cache))


# ---------- Kassir.ru ----------

def kassir_page():
    graph = [{"@type": "WebSite", "name": "kassir"},
             {"@type": "Event", "name": "Ледовое шоу", "startDate": "2026-12-13T13:00:00+00:00", "endDate": "2026-12-13T14:00:00+00:00",
              "url": "https://ufa.kassir.ru/shou/ledovoe-shou", "location": {"@type": "Place", "name": "Уфа-Арена", "address": "Уфа, ул Ленина, 114"},
              "offers": {"@type": "Offer", "price": 1000, "availability": "https://schema.org/InStock"}},
             {"@type": "Event", "name": "Мот", "description": "Мот", "startDate": "2026-11-22T19:00:00+00:00", "url": "https://ufa.kassir.ru/koncert/mot",
              "location": {"@type": "Place", "name": "Дворец борьбы", "address": "Уфа, ул Мусы Гареева, 5"}, "offers": {"@type": "Offer", "price": 4200}}]
    return f'<html><script id="schema-org-graph" type="application/ld+json">{json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)}</script></html>'


def test_kassir_reads_wall_clock_time_and_category_from_the_path():
    records = kassir.parse_section(kassir_page(), city="Уфа", section_url="https://ufa.kassir.ru/", today=TODAY)
    show, mot = records
    assert show["start_at"] == "2026-12-13T13:00:00"  # «+00:00» на сайте ложная метка: на странице «начало в 13:00»
    assert mot["category"] == "concert" and "category" not in show and mot["price_min"] == 4200 and "description" not in mot
    assert show["source"] == "kassir.ru" and show["city"] == "Уфа"


def test_kassir_crawl_skips_a_missing_section_but_stops_on_block():
    pages = {"https://ufa.kassir.ru/robots.txt": 404, "https://ufa.kassir.ru/": kassir_page(), "https://ufa.kassir.ru/bilety-na-koncert": kassir_page()}
    f = fetcher(pages, hosts=kassir.HOSTS, delay=0)
    records = list(kassir.fetch_records(sections=("", "bilety-na-koncert", "bilety-v-teatr"), fetcher=f, today=TODAY))
    assert len(records) == 4  # две страницы по два события; третьей страницы нет (404): пропущена
    blocked = fetcher({**pages, "https://ufa.kassir.ru/bilety-na-koncert": 403}, hosts=kassir.HOSTS, delay=0)
    with pytest.raises(BlockedError):
        list(kassir.fetch_records(sections=("", "bilety-na-koncert"), fetcher=blocked, today=TODAY))


# ---------- KudaGo ----------

KUDAGO_ITEM = {
    "id": 1, "title": "Лебединое озеро", "description": "<p>Балет</p>", "categories": ["theater", "kids"], "price": "от 500 до 1500 рублей", "is_free": False,
    "age_restriction": "6+", "site_url": "https://kudago.com/ufa/event/lebedinoe-ozero/", "images": [{"image": "https://media.kudago.com/1.jpg"}],
    "place": {"title": "Театр оперы и балета", "address": "ул. Ленина, 5/1"},
    "dates": [{"start": 1795000000, "end": 1795003600}, {"start": 1797000000, "end": 1797003600}, {"start": 1700000000, "end": 1700003600}],
}


def test_kudago_prices_and_timestamps():
    assert kudago.parse_price("от 500 до 1500 рублей", False) == (500, 1500)
    assert kudago.parse_price("300 руб.", False) == (300, None)
    assert kudago.parse_price("бесплатно", False) == (0, None) and kudago.parse_price("", True) == (0, None) and kudago.parse_price("по договорённости", False) == (None, None)
    assert kudago.local(1795000000) == datetime(1970, 1, 1, 5, 0) + timedelta(seconds=1795000000)  # метка → местное время Уфы (UTC+5)
    assert kudago.local(0) == datetime(1970, 1, 1, 5, 0)


def test_kudago_item_becomes_one_record_per_upcoming_date():
    records = kudago.to_records(KUDAGO_ITEM, city="Уфа", today=date(2026, 11, 13))
    assert len(records) == 2  # прошлый сеанс (2023) отброшен
    first = records[0]
    assert first["title"] == "Лебединое озеро" and first["description"] == "Балет" and first["category"] == "theatre"
    assert (first["price_min"], first["price_max"], first["age_limit"], first["venue"]) == (500, 1500, 6, "Театр оперы и балета")
    assert first["url"].endswith(f"#{first['start_at'][:10]}") and records[0]["url"] != records[1]["url"]
    assert first["source"] == "kudago.com" and first["source_url"].startswith("https://kudago.com/")
    assert kudago.to_records({**KUDAGO_ITEM, "title": "x"}, city="Уфа", today=TODAY) == []


def test_kudago_paginates_through_the_official_api_without_robots_check():
    first = {"results": [KUDAGO_ITEM], "next": "https://kudago.com/public-api/v1.4/events/?page=2"}
    second = {"results": [{**KUDAGO_ITEM, "id": 2, "title": "Вторая сказка"}], "next": None}
    pages = {}
    f = fetcher(pages, hosts=kudago.HOSTS, delay=0)

    def respond(request, timeout=None):
        url = request.full_url
        f.opener.calls.append(url)
        return FakeResponse(json.dumps(second if "page=2" in url else first), url)

    f.opener.open = respond
    titles = [r["title"] for r in kudago.fetch_records(fetcher=f, today=date(2026, 11, 13))]
    assert titles == ["Лебединое озеро"] * 2 + ["Вторая сказка"] * 2
    assert not any(c.endswith("robots.txt") for c in f.opener.calls)  # открытый API: robots.txt к нему не относится


# ---------- сайты из файла ----------

def test_sites_file_validation(tmp_path):
    assert sites.load_sites(tmp_path / "нет.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text('[{"name": "x"}]', encoding="utf-8")
    with pytest.raises(SourceError, match="name, host и urls"):
        sites.load_sites(bad)
    bad.write_text("{не json", encoding="utf-8")
    with pytest.raises(SourceError, match="не JSON"):
        sites.load_sites(bad)


def test_site_follows_listing_links_and_applies_tz_mode():
    listing = '<a href="/afisha/spektakl-1/">1</a> <a href="/about/">о нас</a> <a href="https://other.ru/afisha/x/">чужой</a> <a href="/afisha/spektakl-2/">2</a>'
    event = lambda name: ld_page({"@type": "TheaterEvent", "name": name, "startDate": "2026-11-27T19:00:00+00:00", "location": {"name": "Театр"}})  # noqa: E731
    pages = {"https://teatr.ru/robots.txt": 404, "https://teatr.ru/afisha/": listing, "https://teatr.ru/afisha/spektakl-1/": event("Первый"), "https://teatr.ru/afisha/spektakl-2/": event("Второй")}
    site = {"name": "teatr.ru", "host": "teatr.ru", "city": "Уфа", "urls": ["https://teatr.ru/afisha/"], "follow": r"^/afisha/[a-z0-9-]+/$", "tz_mode": "local"}
    records = list(sites.fetch_site(site, fetcher=fetcher(pages, hosts=("teatr.ru",), delay=0), today=TODAY))
    assert [r["title"] for r in records] == ["Первый", "Второй"]
    assert records[0]["start_at"] == "2026-11-27T19:00:00" and records[0]["city"] == "Уфа" and records[0]["category"] == "theatre"
    honored = list(sites.fetch_site({**site, "tz_mode": "honor"}, fetcher=fetcher(pages, hosts=("teatr.ru",), delay=0), today=TODAY))
    assert honored[0]["start_at"] == "2026-11-28T00:00:00"  # 19:00 UTC = 00:00 по Уфе: смещение учтено
