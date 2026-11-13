"""Адаптер KudaGo: официальный открытый API (https://docs.kudago.com), поэтому здесь нет разбора HTML.

Города Башкортостана в KudaGo: «ufa» (проверяйте список через /public-api/v1.4/locations/). Время в API задано unix-меткой,
то есть абсолютное, и переводится в местное время Уфы без догадок. Источник просит указывать ссылку на себя: она хранится
в записи (source_url) и показывается в карточке. Если kudago.com недоступен (в России он бывает недоступен), синхронизация
останавливается с понятной ошибкой.
"""
import json
import logging
import re
import time
import urllib.parse
from collections.abc import Iterator
from datetime import date, datetime, timezone

from ..config import LOCAL_TZ, now_local
from ..normalize import canonical_category, parse_age
from .base import Fetcher, SourceError
from .jsonld import clean_text

log = logging.getLogger("afisha.kudago")

SOURCE_NAME = "kudago.com"
API = "https://kudago.com/public-api/v1.4/events/"
HOSTS = ("kudago.com",)
LOCATIONS = {"ufa": "Уфа"}
PAGE_SIZE = 100
MAX_PAGES = 10
FIELDS = "id,title,description,dates,place,categories,price,is_free,age_restriction,site_url,images,tags"
NO_END = 32_000_000_000  # «без конца» в API записывается как год ~3000
CATEGORY = {
    "theater": "theatre", "concert": "concert", "exhibition": "exhibition", "festival": "festival", "kids": "kids",
    "education": "lecture", "cinema": "cinema", "excursion": "excursion", "sport": "sport", "party": "other",
    "entertainment": "other", "quest": "other", "stand-up": "other", "business-events": "lecture", "photo": "exhibition",
}
_PRICE = re.compile(r"\d[\d\s]*")


def local(stamp: int | float) -> datetime:
    return datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(LOCAL_TZ).replace(tzinfo=None)


def parse_price(text: str | None, is_free: bool) -> tuple[int | None, int | None]:
    """«от 500 до 1500 рублей», «300 руб.», «бесплатно» → (500, 1500) / (300, None) / (0, None). Непонятное → (None, None)."""
    if is_free:
        return 0, None
    numbers = [int(re.sub(r"\s", "", n)) for n in _PRICE.findall(text or "") if re.sub(r"\s", "", n)]
    numbers = [n for n in numbers if 0 < n < 1_000_000]
    if "бесплатно" in (text or "").lower() and not numbers:
        return 0, None
    if not numbers:
        return None, None
    return min(numbers), (max(numbers) if len(numbers) > 1 and max(numbers) > min(numbers) else None)


def to_records(item: dict, *, city: str, today: date | None = None) -> list[dict]:
    """Событие KudaGo → записи (по одной на ближайшую дату, чтобы разные дни не склеивались)."""
    today = today or now_local().date()
    title = clean_text(item.get("title"), 300)
    if len(title) < 2:
        return []
    place = item.get("place") if isinstance(item.get("place"), dict) else {}
    low, high = parse_price(item.get("price"), bool(item.get("is_free")))
    category = next((CATEGORY[c] for c in item.get("categories") or [] if c in CATEGORY), None)
    image = next((i.get("image") for i in item.get("images") or [] if isinstance(i, dict) and i.get("image")), "")
    base = {
        "title": title,
        "description": clean_text(item.get("description"), 20_000),
        "category": canonical_category(category, title) if category else None,
        "city": city,
        "venue": clean_text(place.get("title"), 200),
        "address": clean_text(place.get("address"), 300),
        "price_min": low,
        "price_max": high,
        "age_limit": parse_age(item.get("age_restriction")),
        "image_url": image if str(image).startswith("https://") else "",
        "source": SOURCE_NAME,
        "source_url": item.get("site_url") or "",
    }
    records = []
    dates = [d for d in item.get("dates") or [] if isinstance(d, dict) and isinstance(d.get("start"), (int, float))]
    upcoming = []
    for d in dates:
        start = local(d["start"]) if d["start"] > 0 else None
        end = local(d["end"]) if isinstance(d.get("end"), (int, float)) and 0 < d["end"] < NO_END else None
        if start is None:
            continue
        if (end or start).date() < today:
            continue
        if end is not None and end.date() > start.date() and start.date() < today:
            start = datetime.combine(today, datetime.min.time())  # идёт сейчас: началом считаем сегодня
        upcoming.append((start, end))
    for start, end in sorted(upcoming)[:10]:
        record = {**base, "start_at": start.isoformat()}
        if end is not None and end > start:
            record["end_at"] = end.isoformat()
        site = str(item.get("site_url") or "")
        if site.startswith("https://"):
            record["url"] = f"{site}#{start.date().isoformat()}" if len(upcoming) > 1 else site
        records.append({k: v for k, v in record.items() if v not in (None, "")})
    return records


def fetch_records(
    *, locations: dict[str, str] | None = None, fetcher: Fetcher | None = None, today: date | None = None, pages: int = MAX_PAGES
) -> Iterator[dict]:
    locations = locations or LOCATIONS
    fetcher = fetcher or Fetcher(HOSTS, delay=1.0)
    since = int(time.time())
    for slug, city in locations.items():
        query = urllib.parse.urlencode({
            "location": slug, "actual_since": since, "fields": FIELDS, "expand": "place", "text_format": "text",
            "page_size": PAGE_SIZE, "order_by": "dates", "lang": "ru",
        })
        url: str | None = f"{API}?{query}"
        for _ in range(pages):
            if not url:
                break
            try:
                payload = json.loads(fetcher.get(url, robots=False))
            except ValueError as exc:
                raise SourceError(f"KudaGo вернул не JSON: {exc}") from exc
            for item in payload.get("results") or []:
                yield from to_records(item, city=city, today=today)
            url = payload.get("next")
