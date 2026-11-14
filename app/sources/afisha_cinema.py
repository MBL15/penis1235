"""Адаптер кино на Афише (afisha.ru): кинотеатры города и расписание их сеансов.

Как устроено. Страница https://www.afisha.ru/<город>/cinema/cinema_list/ — список кинотеатров города, а расписание
лежит на странице каждого кинотеатра: /<город>/cinema/<кинотеатр>/movie/ (сегодня) и /movie/ДД-ММ-ГГГГ (другой день).
Обе страницы — приложение с готовыми данными в JSON внутри HTML (`window.__nrp ... ['root'] = {...}`), поэтому разбирается
этот JSON, а не вёрстка. robots.txt сайта эти адреса разрешает; перед обходом он всё равно читается, между запросами
выдерживается пауза, капчу и блокировку не обходим (общий Fetcher).

Что считается событием. Одна запись на «фильм × кинотеатр × день»: время начала — ближайший сеанс этого дня, остальные
сеансы перечислены в описании, цена — от самой дешёвой до самой дорогой цены сеансов. Так афиша не раздувается на каждый
сеанс, а цена и время остаются проверяемыми по источнику. Выдуманных данных нет: чего страница не сообщает (описание
фильма, возраст), в записи нет.

Ограничения источника: билеты продаёт и расписание отдаёт сам сервис, поэтому ссылка ведёт на страницу фильма на Афише
(по записи на кинотеатр и день: к ссылке добавляется метка, иначе дедупликация склеила бы разные кинотеатры и дни).
"""
import json
import logging
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta

from ..config import now_local
from ..normalize import parse_age
from .base import BlockedError, Fetcher, SourceError, parse_datetime
from .jsonld import clean_text

log = logging.getLogger("afisha.afisha_cinema")

SOURCE_NAME = "afisha.ru"
HOSTS = ("afisha.ru",)
BASE = "https://www.afisha.ru"

# Города республики, у которых на afisha.ru есть раздел кино (слаг в адресе → название). Остальные города у сайта не
# найдены (404): пропускаются, добавить новый можно одной строкой.
CITY_SLUGS = {"ufa": "Уфа", "sterlitamak": "Стерлитамак", "neftekamsk": "Нефтекамск", "oktyabrskij": "Октябрьский",
              "kumertau": "Кумертау", "birsk": "Бирск", "uchaly": "Учалы"}

MAX_PAGES = 8  # страниц расписания одного кинотеатра за день (по 6 фильмов)

_ROOT = "['root'] = "
_DATE_PARAM = re.compile(r"[?&]date=(\d{2})-(\d{2})-(\d{4})")
_HOURS = re.compile(r"(\d+)\s*ч", re.I)
_MINUTES = re.compile(r"(\d+)\s*мин", re.I)


# ---------- разбор страницы ----------

def page_model(html: str) -> dict:
    """JSON-модель страницы из `window.__nrp[...]['root'] = {"model": {...}}`; JSON-ответ сайта (Accept: application/json) — сама модель."""
    if html.lstrip().startswith("{"):
        try:
            obj = json.loads(html)
        except ValueError as exc:
            raise SourceError(f"JSON-ответ не разобрался: {exc}") from exc
        if not isinstance(obj, dict):
            raise SourceError("в JSON-ответе нет модели данных")
        return obj
    start = html.find(_ROOT)
    if start < 0:
        raise SourceError("в странице нет модели данных (разметка сайта изменилась?)")
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[start + len(_ROOT):])
    except ValueError as exc:
        raise SourceError(f"модель данных не разобралась: {exc}") from exc
    model = obj.get("model") if isinstance(obj, dict) else None
    if not isinstance(model, dict):
        raise SourceError("в модели данных нет раздела model")
    return model


def parse_cinemas(html: str) -> list[dict]:
    """Список кинотеатров города: название, адрес, координаты, адрес страницы расписания."""
    items = ((page_model(html).get("PlaceListWidget") or {}).get("Items")) or []
    cinemas = []
    for item in items:
        if item.get("Type") != "Cinema" or not item.get("Url"):
            continue
        schedule_url = ((item.get("ScheduleUrls") or {}).get("Movie")) or ""
        if not schedule_url:  # у кинотеатра нет расписания фильмов на сайте
            continue
        geo = item.get("GeoPoint") or {}
        address = clean_text(item.get("Address"), 300)
        slug = schedule_url.strip("/").split("/")[0]  # в списке города бывают кинотеатры соседних городов: город берём из адреса страницы
        cinemas.append({
            "id": item.get("ID"), "name": clean_text(item.get("Name"), 200), "address": address,
            "city": CITY_SLUGS.get(slug) or (address.split(",")[0].strip() if "," in address else ""),
            "lat": geo.get("Latitude"), "lon": geo.get("Longitude"), "schedule_url": schedule_url,
        })
    return cinemas


def duration_minutes(text: str | None) -> int | None:
    """«2 часа 20 минут» → 140; «95 минут» → 95; непонятное → None."""
    if not text:
        return None
    hours, mins = _HOURS.search(str(text)), _MINUTES.search(str(text))
    if not hours and not mins:
        return None
    minutes = int(hours.group(1) if hours else 0) * 60 + int(mins.group(1) if mins else 0)
    return minutes if 10 <= minutes <= 600 else None


def _price(value) -> int | None:
    try:
        return int(float(str(value).replace(" ", "").replace(",", ".")))
    except (TypeError, ValueError):
        return None


def _more_pages(pager: dict, cinema: dict, page_url: str, today: date) -> list[str]:
    """Адреса следующих страниц расписания (по 6 фильмов). HTML отдаёт только первую, остальные сайт подгружает запросом к той же
    странице с `Accept: application/json` и параметрами `date=ГГГГ-ММ-ДД--ГГГГ-ММ-ДД&page=N&pageSize=6`; адреса `/pageN` не работают."""
    count, size = pager.get("PagesCount") or 1, pager.get("PageSize") or 6
    if (pager.get("CurrentPage") or 1) != 1 or not isinstance(count, int) or not isinstance(size, int) or count < 2:
        return []
    found = _DATE_PARAM.search(page_url)
    day = date(int(found.group(3)), int(found.group(2)), int(found.group(1))) if found else today
    base = BASE + cinema["schedule_url"] if cinema["schedule_url"].startswith("/") else cinema["schedule_url"]
    return [f"{base}?date={day:%Y-%m-%d}--{day:%Y-%m-%d}&page={n}&pageSize={size}" for n in range(2, min(count, MAX_PAGES) + 1)]


def parse_schedule(html: str, cinema: dict, *, city: str, page_url: str, now: datetime | None = None) -> tuple[list[dict], list[str]]:
    """Страница расписания кинотеатра на день → (записи, адреса остальных страниц этого же расписания)."""
    now = now or now_local()
    model = page_model(html)
    widget = (model.get("Schedule") or {}).get("MovieWidget") or {}
    more = _more_pages(widget.get("Pager") or {}, cinema, page_url, now.date())
    city = cinema.get("city") or city
    records = []
    for item in widget.get("Items") or []:
        movie = item.get("Movie") or {}
        title = clean_text(movie.get("Name"), 300)
        if len(title) < 2:
            continue
        sessions = []
        for session in item.get("Sessions") or []:
            moment = parse_datetime(session.get("DateTime"), tz_mode="local")  # время местное, как на сайте
            if moment is None or session.get("Started") or moment < now:
                continue
            sessions.append((moment, session))
        if not sessions:  # все сеансы этого дня уже начались
            continue
        sessions.sort(key=lambda s: s[0])
        first = sessions[0][0]
        prices = [p for p in (_price(s.get("MinPriceFormatted")) for _, s in sessions) if p is not None]
        minutes = duration_minutes(movie.get("Duration"))

        genres = [g.get("Name") for g in ((movie.get("Genres") or {}).get("Links") or []) if g.get("Name")]
        facts = [", ".join(genres), ", ".join(p for p in (movie.get("Country"), str(movie.get("ProductionYear") or "")) if p),
                 f"Длительность: {movie['Duration']}" if movie.get("Duration") else ""]
        times = ", ".join(f"{moment:%H:%M}" + (f" ({s['Title'].replace('Сеанс в формате ', '')})" if "формате" in str(s.get("Title")) else "")
                          for moment, s in sessions)
        description = " ".join(
            part for part in (". ".join(f for f in facts if f) + "." if any(facts) else "", clean_text(movie.get("Synopsis"), 1500),
                              f"Сеансы {first:%d.%m}: {times}.") if part)

        film_url = str(movie.get("Url") or "").strip()
        film_url = BASE + "/" + film_url.lstrip("/") if film_url else page_url
        film_url = film_url.split("#")[0]
        record = {
            "title": title,
            "description": description,
            "start_at": first.isoformat(),
            "category": "cinema",
            "city": city,
            "venue": cinema["name"],
            "address": (cinema["address"] if cinema["address"].lower().startswith(city.lower()) else f"{city}, {cinema['address']}")
            if cinema.get("address") else "",
            "price_min": min(prices) if prices else None,
            "price_max": max(prices) if prices else None,
            "url": f"{film_url}#kino-{cinema['id']}-{first:%Y%m%d}",
            "image_url": ((movie.get("Poster") or {}).get("Url")) or "",
            "source": SOURCE_NAME,
            "source_url": page_url,
            "lat": cinema.get("lat"),
            "lon": cinema.get("lon"),
        }
        age = parse_age(movie.get("AgeRestriction"))
        if age is not None:
            record["age_limit"] = age
        if minutes:
            record["end_at"] = (first + timedelta(minutes=minutes)).isoformat()
        records.append({k: v for k, v in record.items() if v not in (None, "")})
    return records, more


# ---------- обход ----------

def _day_url(schedule_url: str, day: date, today: date) -> str:
    """Адрес расписания на день. Сайт понимает день только в параметре `?date=ДД-ММ-ГГГГ`: адрес вида `/movie/ДД-ММ-ГГГГ`,
    который он сам же показывает в календаре, отдаёт расписание на сегодня;."""
    base = (BASE + schedule_url if schedule_url.startswith("/") else schedule_url).rstrip("/") + "/"
    return base if day == today else f"{base}?date={day:%d-%m-%Y}"


def fetch_records(
    *,
    cities: dict[str, str] | None = None,
    days: int = 3,
    fetcher: Fetcher | None = None,
    now: datetime | None = None,
) -> Iterator[dict]:
    """Записи кино: для каждого города кинотеатры, для каждого кинотеатра расписание на `days` дней, начиная с сегодняшнего."""
    cities = cities or {"ufa": CITY_SLUGS["ufa"]}
    fetcher = fetcher or Fetcher(HOSTS, delay=1.5)
    now = now or now_local()
    today = now.date()
    for slug, city in cities.items():
        try:
            cinemas = parse_cinemas(fetcher.get(f"{BASE}/{slug}/cinema/cinema_list/"))
        except BlockedError:
            raise
        except SourceError as exc:
            log.warning("Афиша (кино): %s: %s", city, exc)
            continue
        log.info("Афиша (кино): %s, кинотеатров %d", city, len(cinemas))
        for cinema in cinemas:
            for offset in range(max(1, days)):
                url = _day_url(cinema["schedule_url"], today + timedelta(days=offset), today)
                try:
                    records, more = parse_schedule(fetcher.get(url), cinema, city=city, page_url=url, now=now)
                except BlockedError:
                    raise
                except SourceError as exc:
                    log.warning("Афиша (кино): %s", exc)  # одна страница не открылась: идём дальше
                    continue
                yield from records
                for more_url in more:  # остальные страницы дня (по 6 фильмов) — JSON той же страницы
                    try:
                        extra, _ = parse_schedule(fetcher.get(more_url, accept="application/json"), cinema, city=city, page_url=more_url, now=now)
                    except BlockedError:
                        raise
                    except SourceError as exc:
                        log.warning("Афиша (кино): %s", exc)
                        continue
                    yield from extra
