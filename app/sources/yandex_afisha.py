"""Адаптер Яндекс Афиши (afisha.yandex.ru).

Как это работает и какие у этого границы:
* страницы событий находятся по sitemap.xml (его сайт публикует для обходчиков), а не по закрытому /api (robots.txt его запрещает);
* на странице события есть schema.org (JSON-LD) и Apollo-состояние страницы (window['__APOLLO_STATE__']): в нём расписание,
  возрастное ограничение и тип события. Дата и время в JSON-LD НЕ годятся: там период показа в UTC, а не сеанс
  (например «спектакль 29 ноября 19:00» там записан как 28 ноября 21:00Z), поэтому сеансы берутся из Apollo-состояния;
* из расписания известны даты ближайших сеансов; время сеанса известно только для даты, которую страница показывает
  первой, для остальных берётся то же время, если оно у сеансов одинаковое, иначе остаётся только дата;
* постоянные выставки и экскурсии (ежедневные даты) становятся одной записью «с … по …»;
* капчу и блокировку не обходим: при 403/429/капче обход останавливается;
* условия использования сайта разрешают ли автоматический сбор, стоит уточнить до публичного запуска.
"""
import json
import logging
import re
import time
from collections.abc import Iterator
from datetime import date, datetime, time as dtime
from pathlib import Path

from ..config import BASE_DIR, now_local
from ..normalize import canonical_category, parse_age
from .base import BlockedError, Fetcher, SourceError
from .jsonld import extract_events, record_from_ld

log = logging.getLogger("afisha.yandex")

SOURCE_NAME = "afisha.yandex.ru"
BASE = "https://afisha.yandex.ru"
HOSTS = ("afisha.yandex.ru",)

# Города Башкортостана в адресах Яндекс Афиши (slug → название). Нет события в sitemap: город просто пропускается.
CITY_SLUGS = {
    "ufa": "Уфа", "sterlitamak": "Стерлитамак", "salavat": "Салават", "neftekamsk": "Нефтекамск",
    "oktyabrskiy": "Октябрьский", "sibay": "Сибай", "beloretsk": "Белорецк", "kumertau": "Кумертау",
    "meleuz": "Мелеуз", "ishimbay": "Ишимбай", "tuymazy": "Туймазы", "birsk": "Бирск",
}
# Рубрики, которые не афиша культуры (кинопрокат: сотни фильмов с сеансами) или служебные страницы.
SKIP_RUBRICS = {"selections", "places", "tag", "cinema", "persons", "afisha", "api"}
RUBRIC_CATEGORY = {
    "concert": "concert", "theatre": "theatre", "theatre_show": "theatre", "ballet": "theatre", "opera": "theatre",
    "musical": "theatre", "musical-play": "theatre", "art": "exhibition", "excursions": "excursion",
    "masterclass": "lecture", "lectures": "lecture", "kids": "kids", "sport": "sport", "festival": "festival",
}
MAX_DATES = 10  # сеансов одного события на ближайшие даты: дальше события и так будут обновляться
DAILY_THRESHOLD = 14  # больше дат подряд: это постоянная выставка/экскурсия, а не расписание сеансов
_DATE_SLUG = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)-\d+$")
_LOC = re.compile(r"<url>\s*<loc>([^<]+)</loc>(?:\s*<lastmod>([^<]+)</lastmod>)?", re.S)
CACHE_FILE = BASE_DIR / "data" / "yandex_afisha_urls.json"


# ---------- поиск страниц событий ----------

def event_url_city(url: str, cities: dict[str, str]) -> str | None:
    """Адрес страницы события (город/рубрика/событие) → slug города; подборки, даты, места, кино и чужие города → None."""
    parts = url[len(BASE) + 1:].strip("/").split("/") if url.startswith(BASE + "/") else []
    if len(parts) == 3 and parts[0] in cities and parts[1] not in SKIP_RUBRICS and not _DATE_SLUG.match(parts[2]):
        return parts[0]
    return None


def event_urls_from_sitemap(xml: str, cities: dict[str, str]) -> dict[str, str]:
    """Страницы событий из sitemap: рубрика/slug (три сегмента: город, рубрика, событие)."""
    found: dict[str, str] = {}
    for url, lastmod in _LOC.findall(xml):
        if event_url_city(url, cities):
            found[url] = lastmod or ""
    return found


def discover(fetcher: Fetcher, cities: dict[str, str], cache: Path = CACHE_FILE, max_age: float = 24 * 3600) -> dict[str, str]:
    """Адреса страниц событий нужных городов. Список sitemap тяжёлый (~80 МБ), поэтому результат кэшируется на сутки."""
    key = ",".join(sorted(cities))
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("cities") == key and time.time() - cached.get("at", 0) < max_age:
            return cached["urls"]
    except (OSError, ValueError, KeyError):
        pass
    index = fetcher.get(BASE + "/sitemap.xml")
    sitemaps = re.findall(r"<loc>([^<]+)</loc>", index)
    if not sitemaps:
        raise SourceError("в sitemap.xml нет ссылок на карты сайта: разметка Яндекс Афиши изменилась")
    urls: dict[str, str] = {}
    for sitemap in sitemaps:
        urls.update(event_urls_from_sitemap(fetcher.get(sitemap), cities))
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"cities": key, "at": time.time(), "urls": urls}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        log.warning("не удалось сохранить кэш адресов %s", cache)
    return urls


# ---------- разбор страницы события ----------

def apollo_state(html: str) -> dict:
    match = re.search(r"window\['__APOLLO_STATE__'\]\s*=\s*", html)
    if not match:
        raise SourceError("на странице нет состояния __APOLLO_STATE__: разметка Яндекс Афиши изменилась")
    try:
        state, _ = json.JSONDecoder().raw_decode(html[match.end():])
    except ValueError as exc:
        raise SourceError(f"не разобрать __APOLLO_STATE__: {exc}") from exc
    return state


def _root(state: dict, prefix: str) -> dict:
    root = state.get("ROOT_QUERY") or {}
    return next((v for k, v in root.items() if k.startswith(prefix) and isinstance(v, dict)), {})


def schedule(state: dict) -> dict:
    """Расписание из Apollo-состояния: даты, признак «постоянно», время сеансов по датам."""
    info = _root(state, "eventScheduleInfo")
    sessions: dict[str, dtime] = {}
    other = _root(state, "eventScheduleOther")
    for group in other.get("byDate") or []:
        for item in group.get("sessions") or []:
            stamp = (item.get("session") or {}).get("datetime")
            if stamp:
                moment = datetime.fromisoformat(stamp)
                sessions.setdefault(moment.date().isoformat(), moment.time().replace(second=0, microsecond=0))
    regularity = info.get("regularity") or {}
    single = regularity.get("singleShowtime")
    if single:
        moment = datetime.fromisoformat(single)
        sessions.setdefault(moment.date().isoformat(), moment.time().replace(second=0, microsecond=0))
    return {
        "dates": [d for d in info.get("dates") or [] if isinstance(d, str)],
        "permanent": bool(info.get("permanent")),
        "started": info.get("dateStarted"),
        "ended": info.get("dateEnd"),
        "times": sessions,
    }


def session_starts(plan: dict, today: date) -> list[tuple[datetime, datetime | None]]:
    """Расписание → список (начало, конец). Постоянные события: одна запись с периодом, обычные: по записи на дату."""
    dates = sorted(d for d in plan["dates"] if d >= today.isoformat())
    if not dates:
        return []
    if plan["permanent"] or len(dates) > DAILY_THRESHOLD:
        first = max(date.fromisoformat(dates[0]), today)
        return [(datetime.combine(first, dtime.min), datetime.combine(date.fromisoformat(plan["ended"] or dates[-1]), dtime.max.replace(microsecond=0)))]
    times = plan["times"]
    common = next(iter(set(times.values()))) if len(set(times.values())) == 1 else None
    out = []
    for iso in dates[:MAX_DATES]:
        moment = times.get(iso) or common or dtime.min
        out.append((datetime.combine(date.fromisoformat(iso), moment), None))
    return out


def _event_object(state: dict) -> dict:
    return next((v for k, v in state.items() if k.startswith("Event:") and isinstance(v, dict)), {})


def parse_event_page(html: str, url: str, *, city: str, today: date | None = None) -> list[dict]:
    """Страница события → записи для app.ingest: по записи на ближайшую дату (или одна запись для постоянных)."""
    today = today or now_local().date()
    state = apollo_state(html)
    event = _event_object(state)
    lds = extract_events(html)
    if not event or not lds:
        raise SourceError(f"{url}: нет события в разметке страницы")
    # Базовая запись из JSON-LD (название, площадка, цена, статус, картинка); даты и время берём из расписания.
    base = record_from_ld({**lds[0], "startDate": "2000-01-01T00:00:00", "endDate": None}, source=SOURCE_NAME, default_city=city,
                          page_url=url, today=date(1999, 1, 1))
    if base is None:
        raise SourceError(f"{url}: не разобрать событие")
    base.pop("start_at", None)
    base.pop("end_at", None)
    base["title"] = (event.get("title") or base["title"]).strip()
    rubric = ((event.get("type") or {}).get("code")) or url.rsplit("/", 2)[-2]
    base["category"] = RUBRIC_CATEGORY.get(rubric) or canonical_category(None, base["title"])
    age = parse_age(event.get("contentRating"))
    if age:
        base["age_limit"] = age
    plan = schedule(state)
    starts = session_starts(plan, today)
    records = []
    for start, end in starts:
        record = {**base, "start_at": start.isoformat()}
        if end is not None:
            record["end_at"] = end.isoformat()
        if len(starts) > 1 or plan["permanent"]:
            # Одна страница, много дат: у каждой записи свой адрес, иначе дедупликация по ссылке склеила бы разные дни.
            record["url"] = f"{url}#{start.date().isoformat()}"
        records.append(record)
    return records


# ---------- обход ----------

def fetch_records(
    limit: int = 150,
    *,
    cities: dict[str, str] | None = None,
    fetcher: Fetcher | None = None,
    today: date | None = None,
    state_path: Path | None = None,
    cache: Path = CACHE_FILE,
) -> Iterator[dict]:
    """Записи со страниц событий. limit: сколько страниц обойти за запуск (страницы идут от недавно обновлённых)."""
    cities = cities or {"ufa": CITY_SLUGS["ufa"]}
    fetcher = fetcher or Fetcher(HOSTS, delay=1.2)
    urls = discover(fetcher, cities, cache)
    ordered = sorted(urls, key=lambda u: urls[u], reverse=True)[:limit]
    log.info("Яндекс Афиша: страниц событий %d, обходим %d", len(urls), len(ordered))
    for url in ordered:
        city = cities[event_url_city(url, cities)]
        try:
            yield from parse_event_page(fetcher.get(url), url, city=city, today=today)
        except BlockedError:
            raise  # сайт просит остановиться: останавливаемся
        except SourceError as exc:
            log.warning("Яндекс Афиша: %s", exc)  # одна страница не разобралась или закрыта robots.txt: идём дальше
