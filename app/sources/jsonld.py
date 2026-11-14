"""Разбор событий в формате schema.org (JSON-LD): его отдают билетные сервисы, афиши и сайты театров, чтобы поисковики
понимали события. Один разбор покрывает много сайтов: адаптеру остаётся найти страницы и выбрать режим часового пояса."""
import html as html_lib
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..config import now_local
from ..normalize import CONTINUOUS_CATEGORIES, canonical_category, find_city_in_text
from .base import parse_datetime

_LD_SCRIPT = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")

# schema.org: подтипы Event, которые встречаются на афишах → наши категории
TYPE_CATEGORY = {
    "MusicEvent": "concert",
    "TheaterEvent": "theatre",
    "DanceEvent": "theatre",
    "ExhibitionEvent": "exhibition",
    "VisualArtsEvent": "exhibition",
    "ChildrensEvent": "kids",
    "Festival": "festival",
    "SportsEvent": "sport",
    "ScreeningEvent": "cinema",
    "EducationEvent": "lecture",
    "LiteraryEvent": "lecture",
}
STATUS = {
    "eventcancelled": "cancelled",
    "eventpostponed": "rescheduled",
    "eventrescheduled": "rescheduled",
}


def clean_text(value, limit: int) -> str:
    """Текст из разметки: без тегов и html-сущностей, лишние пробелы схлопнуты."""
    if not value:
        return ""
    text = html_lib.unescape(_TAGS.sub(" ", str(value)))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()[:limit]


def extract_ld(html: str) -> list[dict]:
    """Все объекты JSON-LD страницы, с раскрытыми @graph и списками. Битый JSON пропускается."""
    found: list[dict] = []

    def add(node) -> None:
        if isinstance(node, list):
            for item in node:
                add(item)
        elif isinstance(node, dict):
            found.append(node)
            if "@graph" in node:
                add(node["@graph"])

    for match in _LD_SCRIPT.finditer(html):
        try:
            add(json.loads(match.group(1).strip()))
        except ValueError:
            continue
    return found


def _types(node: dict) -> list[str]:
    value = node.get("@type")
    return [str(t).rsplit("/", 1)[-1] for t in (value if isinstance(value, list) else [value]) if t]


def is_event(node: dict) -> bool:
    return any(t in TYPE_CATEGORY or t.endswith("Event") or t == "Festival" for t in _types(node))


def extract_events(html: str) -> list[dict]:
    return [n for n in extract_ld(html) if is_event(n)]


def _first(value):
    return value[0] if isinstance(value, list) and value else value


def _price(value) -> int | None:
    try:
        number = Decimal(str(value).replace(",", ".").replace(" ", ""))
    except (InvalidOperation, ValueError):
        return None
    # «NaN» и «Infinity» тоже разбираются как Decimal, а сравнение с NaN бросает InvalidOperation и роняло разбор страницы
    return int(number) if number.is_finite() and 0 <= number <= 10_000_000 else None


def prices_from_offers(offers) -> tuple[int | None, int | None, bool]:
    """(цена от, цена до, всё распродано) по блоку offers: один Offer, AggregateOffer или список."""
    lows: list[int] = []
    highs: list[int] = []
    sold_out = []
    for offer in offers if isinstance(offers, list) else [offers]:
        if not isinstance(offer, dict):
            continue
        for key, bucket in (("price", lows), ("lowPrice", lows), ("price", highs), ("highPrice", highs)):
            price = _price(offer[key]) if key in offer else None
            if price is not None:
                bucket.append(price)
        availability = str(offer.get("availability", "")).lower()
        if availability:
            sold_out.append("soldout" in availability or "outofstock" in availability)
    low = min(lows) if lows else None
    high = max(highs) if highs else None
    return low, (high if high is not None and low is not None and high >= low else None), bool(sold_out) and all(sold_out)


def place_info(location) -> tuple[str, str, str | None]:
    """(площадка, адрес, город) из location: Place, список мест, строка или адрес."""
    location = _first(location)
    if isinstance(location, str):
        return clean_text(location, 200), "", find_city_in_text(location)
    if not isinstance(location, dict):
        return "", "", None
    name = clean_text(location.get("name"), 200)
    address = location.get("address")
    city = None
    if isinstance(address, dict):
        street = clean_text(address.get("streetAddress"), 300)
        city = clean_text(address.get("addressLocality"), 64) or None
        address = street
    else:
        address = clean_text(address, 300)
        city = find_city_in_text(address)
    return name, address, city


def record_from_ld(
    node: dict,
    *,
    source: str,
    default_city: str | None = None,
    tz_mode: str = "honor",
    page_url: str = "",
    today: date | None = None,
    default_category: str | None = None,
    period_as_series: bool = False,
) -> dict | None:
    """Событие schema.org → запись для app.ingest (формат EventIn). None: нет названия/даты или событие уже прошло.

    default_category: категория, если по типам schema.org она не определилась (адаптер знает её по адресу страницы).
    period_as_series: сайт записывает серию показов как период startDate…endDate (так делает Kassir.ru). Тогда для всех
    категорий, кроме непрерывных (выставки, экскурсии, фестивали), конец периода уходит в series_until, а start_at остаётся
    известным показом: серия не должна «идти» каждый день периода."""
    today = today or now_local().date()
    title = clean_text(node.get("name"), 300)
    start = parse_datetime(node.get("startDate"), tz_mode=tz_mode)
    if len(title) < 2 or start is None:
        return None
    end = parse_datetime(node.get("endDate"), tz_mode=tz_mode)
    if end is not None and end < start:
        end = None
    if (end or start).date() < today:
        return None

    types = _types(node)
    category = next((TYPE_CATEGORY[t] for t in types if t in TYPE_CATEGORY), None) or default_category
    description = clean_text(node.get("description"), 20_000)
    if description == title:  # некоторые сервисы кладут в описание само название
        description = ""

    multi_day = end is not None and end.date() > start.date()
    series = (
        period_as_series and multi_day
        and canonical_category(category, f"{title} {description[:300]}") not in CONTINUOUS_CATEGORIES
    )
    # Непрерывное многодневное событие, которое уже идёт: началом считаем сегодня (как делает адаптер culture.ru).
    if multi_day and not series and start.date() < today:
        start = datetime.combine(today, datetime.min.time())

    venue, address, city = place_info(node.get("location"))
    place = _first(node.get("location"))
    geo = place.get("geo") if isinstance(place, dict) else None
    lat = lon = None
    if isinstance(geo, dict):
        try:
            lat, lon = float(geo.get("latitude")), float(geo.get("longitude"))
        except (TypeError, ValueError):
            lat = lon = None
        if lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
            lat = lon = None
    low, high, sold_out = prices_from_offers(node.get("offers"))
    status = STATUS.get(str(node.get("eventStatus", "")).rsplit("/", 1)[-1].lower(), "sold_out" if sold_out else "scheduled")
    url = str(node.get("url") or page_url or "")
    image = _first(node.get("image"))
    if isinstance(image, dict):
        image = image.get("contentUrl") or image.get("url")

    record = {
        "title": title,
        "description": description,
        "start_at": start.isoformat(),
        "category": canonical_category(category, f"{title} {description[:300]}") if category else None,
        "city": city or default_city,
        "venue": venue,
        "address": address,
        "price_min": low,
        "price_max": high,
        "url": url if url.lower().startswith("https://") or url.lower().startswith("http://") else "",
        "image_url": str(image) if image and str(image).lower().startswith("http") else "",
        "status": status,
        "source": source,
        "source_url": url,
        "lat": lat,
        "lon": lon,
    }
    if end is not None:
        record["series_until" if series else "end_at"] = end.isoformat()
    return {k: v for k, v in record.items() if v not in (None, "")}
