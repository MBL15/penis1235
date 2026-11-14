"""Адаптер афиши culture.ru («Афиша Республики Башкортостан»).

Страница афиши — Next.js: данные карточек лежат в HTML, в теге <script id="__NEXT_DATA__">, по 20 штук на страницу.
Листаем через ?page=N. Пути /afisha/... и параметр page разрешены в robots.txt сайта; перед обходом это проверяется,
между страницами выдерживается пауза, а запросы идут с честным User-Agent.

Ограничения источника, о которых нужно помнить:
* в карточке только ближайший сеанс события, а не всё расписание;
* статусов (отмена, перенос) и описаний в списке нет: поля остаются пустыми, мы ничего не выдумываем;
* у постоянных выставок есть только дата окончания, поэтому началом считаем день синхронизации.
"""
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, time as dtime

from ..config import LOCAL_TZ, PUBLIC_URL, now_local
from ..normalize import canonical_category, find_city_in_text
from .robots import Robots
from .base import SourceError  # noqa: F401  (общий тип ошибки всех источников; импортируется отсюда и в тестах)

log = logging.getLogger("afisha.culture_ru")

SOURCE_NAME = "culture.ru"
BASE = "https://www.culture.ru"
LISTING = BASE + "/afisha/respublika-bashkortostan"
USER_AGENT = f"bashafisha-hackathon/1.0 (+{PUBLIC_URL})"
PAGE_SIZE = 20
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # страница афиши весит около 300 КБ: больше 5 МБ точно не то, что мы ждём
ALLOWED_HOSTS = ("www.culture.ru", "culture.ru")

GENRE_CATEGORY = {
    "Выставки": "exhibition",
    "Концерты": "concert",
    "Спектакли": "theatre",
    "Обучение": "lecture",
    "Встречи": "lecture",
}

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


@dataclass
class ParsedPage:
    items: list[dict]
    total_pages: int


def parse_page(html: str) -> ParsedPage:
    match = _NEXT_DATA.search(html)
    if not match:
        raise SourceError("в странице нет __NEXT_DATA__: разметка culture.ru изменилась")
    try:
        events = json.loads(match.group(1))["props"]["pageProps"]["events"]
        return ParsedPage(items=events["items"], total_pages=int(events["pagination"]["total"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError(f"неожиданная структура данных culture.ru: {exc!r}") from exc


def _utc_to_local(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(LOCAL_TZ).replace(tzinfo=None)


def _category(item: dict) -> str:
    genres = [g.get("title", "") for g in item.get("genres") or []]
    for title in genres:
        if title in GENRE_CATEGORY:
            return GENRE_CATEGORY[title]
    return canonical_category(genres[0] if genres else None, item.get("title", ""))


def _place(item: dict) -> dict:
    return item.get("selectedLocalePlace") or item.get("nearestSeancePlace") or next(iter(item.get("places") or []), {})


def _city(place: dict) -> str | None:
    title = ((place.get("locale") or {}).get("title") or "").strip()
    # Для событий «по региону» locale бывает не городом: тогда берём город из адреса.
    if not title or title.startswith(("Республика", "Россия")):
        return find_city_in_text(place.get("address", ""))
    return title


def to_record(item: dict, today: date | None = None) -> dict:
    """Карточка culture.ru → запись для app.ingest (формат EventIn)."""
    today = today or now_local().date()
    schedule = item.get("cardSchedule") or {}
    mode = schedule.get("mode")
    primary = _utc_to_local(schedule["primaryDate"])
    ended = _utc_to_local(item["seanceEndDate"]) if item.get("seanceEndDate") else None

    if mode == "until":  # «до 30 дек»: начала нет, есть только конец
        start, end = datetime.combine(today, dtime.min), primary
    elif mode == "from":  # «с 06 дек»: серия до seanceEndDate
        start, end = primary, ended
    else:  # single: seanceEndDate — конец сеанса, нужен только в пределах одного дня
        start, end = primary, ended if ended and ended.date() == primary.date() and ended > primary else None

    place = _place(item)
    price = item.get("price") or {}
    event_path = f"{item['renderId']}/{item['name']}"
    url = f"{BASE}/events/{event_path}"
    image_id = (item.get("thumbnailFile") or {}).get("publicId")

    record = {
        "title": item["title"].strip(),
        "start_at": start.isoformat(),
        "category": _category(item),
        "city": _city(place),
        "venue": item.get("topPlaceTitle") or place.get("title") or "",
        "address": place.get("address") or "",
        "price_min": price.get("min"),
        "price_max": price.get("max"),
        "age_limit": item.get("ageRestriction"),
        "url": url,
        "image_url": f"https://cdn.culture.ru/images/{image_id}" if image_id else "",
        "source": SOURCE_NAME,
        "source_url": url,
    }
    if end is not None:
        record["end_at"] = end.isoformat()
    return {k: v for k, v in record.items() if v is not None}


# ---------- сеть ----------

def http_get(url: str, *, retries: int = 5, timeout: float = 20.0) -> str:
    """GET с повторами: соединение с сайтом бывает нестабильным (TLS-рукопожатие иногда зависает)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ru"})
    # По умолчанию идём напрямую: российский сайт через системный (часто зарубежный) прокси недоступен.
    # Нужен прокси — задайте AFISHA_USE_PROXY=1, тогда возьмутся HTTP(S)_PROXY из окружения.
    opener = urllib.request.build_opener() if os.getenv("AFISHA_USE_PROXY") == "1" else urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    )
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with opener.open(request, timeout=timeout) as response:
                # После перенаправлений мы всё ещё должны быть на сайте источника, и по HTTPS.
                final = urllib.parse.urlsplit(response.geturl())
                if final.scheme != "https" or final.hostname not in ALLOWED_HOSTS:
                    raise SourceError(f"{url}: перенаправление на посторонний адрес {response.geturl()}")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise SourceError(f"{url}: ответ больше {MAX_RESPONSE_BYTES // 1024 // 1024} МБ")
                return body.decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(min(2 * (attempt + 1), 8))
    raise SourceError(f"{url}: {last}")


def ensure_allowed(url: str, fetch: Callable[[str], str] = http_get) -> None:
    """Не ходим туда, куда сайт просит не ходить. Не удалось прочитать robots.txt: останавливаемся."""
    if not Robots(fetch(BASE + "/robots.txt")).can_fetch(USER_AGENT, url):
        raise SourceError(f"robots.txt culture.ru запрещает обход {url}")


def fetch_records(
    max_pages: int = 10,
    delay: float = 1.5,
    *,
    fetch: Callable[[str], str] = http_get,
    sleep: Callable[[float], None] = time.sleep,
    today: date | None = None,
) -> Iterator[dict]:
    """Постранично отдаёт записи. Страницы идут от ближайших дат к дальним."""
    ensure_allowed(LISTING, fetch)
    page, total = 1, max_pages
    while page <= min(max_pages, total):
        parsed = parse_page(fetch(f"{LISTING}?page={page}"))
        total = parsed.total_pages
        for item in parsed.items:
            try:
                yield to_record(item, today)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                # Одна карточка с неожиданной структурой не должна обрывать загрузку остальных.
                log.warning("culture.ru: карточка %r пропущена: %r", item.get("title") if isinstance(item, dict) else item, exc)
        if not parsed.items:
            break
        page += 1
        if page <= min(max_pages, total):
            sleep(delay)
