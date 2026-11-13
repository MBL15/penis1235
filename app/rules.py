"""Разбор запроса на русском без ИИ: запасной режим, когда нет ключа Gemini или он недоступен."""
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .formatting import MONTHS_GEN, WEEKDAYS_FULL
from .normalize import CATEGORIES, CATEGORY_STEMS, KIDS_STEMS, find_city_in_text, norm_text
from .search import SearchFilters, resolve_when

WEEKDAY_STEMS = ("понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень")
MONTH_MAP = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
FAMILY_WORDS = KIDS_STEMS + ("сын", "доч", "внук", "племянн")

_PRICE_RE = re.compile(
    r"(?:до|дешевле|не дороже|не более чем|не более|максимум|в пределах)\s+(\d+(?:\s\d{3})*)(?:\s*(тысяч|тыс|к)\b)?"
)
_AGE_RE = re.compile(r"(\d{1,2})\s*(?:лет|года|год|летн)")
_DATE_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(sorted(MONTH_MAP, key=len, reverse=True)) + r")\w*")


@dataclass
class Parsed:
    filters: SearchFilters
    notes: list[str] = field(default_factory=list)  # что мы поняли — показываем пользователю
    recognized: bool = False


def _next_weekday(today: date, target: int) -> date:
    return today + timedelta(days=(target - today.weekday()) % 7)


def _day_label(d: date, today: date) -> str:
    full = f"{WEEKDAYS_FULL[d.weekday()]}, {d.day} {MONTHS_GEN[d.month - 1]}"
    if d == today:
        return f"сегодня, {full}"
    if d == today + timedelta(days=1):
        return f"завтра, {full}"
    return full


def describe(f: SearchFilters, today: date) -> list[str]:
    """Условия поиска человеческим языком: «суббота, 8 ноября», «до 1000 ₽», «для детей»."""
    notes: list[str] = []
    if f.date_from and f.date_to:
        if f.date_from == f.date_to:
            notes.append(_day_label(f.date_from, today))
        else:
            a, b = f.date_from, f.date_to
            notes.append(f"{a.day} {MONTHS_GEN[a.month - 1]} – {b.day} {MONTHS_GEN[b.month - 1]}")
    if f.kids:
        notes.append(f"для ребёнка {f.age} лет" if f.age is not None else "для детей")
    if f.free_only:
        notes.append("бесплатно")
    elif f.price_max is not None:
        notes.append(f"до {f.price_max} ₽")
    if f.city:
        notes.append(f.city)
    if f.category:
        notes.append(" или ".join(CATEGORIES.get(c, c).lower() for c in f.category.split(",")))
    return notes


def parse_query(text: str, today: date) -> Parsed:
    low = norm_text(text)
    words = low.split()
    f = SearchFilters(limit=8)

    # --- когда ---
    explicit = _DATE_RE.search(low)
    if explicit:
        month, day = MONTH_MAP[explicit.group(2)], int(explicit.group(1))
        try:
            d = date(today.year, month, day)
            if d < today:
                d = date(today.year + 1, month, day)
            f.date_from = f.date_to = d
        except ValueError:
            pass
    elif "послезавтра" in words:
        f.date_from = f.date_to = today + timedelta(days=2)
    elif "завтра" in words:
        f.date_from, f.date_to = resolve_when("tomorrow", today)
    elif "сегодня" in words:
        f.date_from, f.date_to = resolve_when("today", today)
    elif any(w.startswith("выходн") for w in words):
        f.date_from, f.date_to = resolve_when("weekend", today)
    elif any(w.startswith("недел") for w in words):
        if any(w.startswith("следующ") for w in words):
            monday = today + timedelta(days=7 - today.weekday())
            f.date_from, f.date_to = monday, monday + timedelta(days=6)
        else:
            f.date_from, f.date_to = resolve_when("week", today)
    else:
        for word in words:
            index = next((i for i, s in enumerate(WEEKDAY_STEMS) if word.startswith(s) and not word.startswith("средн")), None)
            if index is not None:
                f.date_from = f.date_to = _next_weekday(today, index)
                break

    # --- с кем ---
    if any(w.startswith(FAMILY_WORDS) for w in words):
        f.kids = True
        age = _AGE_RE.search(low)
        if age:
            f.age = int(age.group(1))

    # --- сколько ---
    if any(w.startswith(("бесплатн", "даром", "free")) for w in words) or "без оплаты" in low:
        f.free_only = True
    else:
        price = _PRICE_RE.search(low)
        # «до 5 лет» — это возраст, а не цена
        if price and not _AGE_RE.match(low[price.start(1):]):
            amount = int(price.group(1).replace(" ", ""))
            f.price_max = amount * 1000 if price.group(2) else amount

    # --- где и что ---
    f.city = find_city_in_text(text)
    for slug, stems in CATEGORY_STEMS.items():
        if slug != "kids" and any(w.startswith(stems) for w in words):
            f.category = slug
            break

    recognized = bool(f.date_from or f.kids or f.free_only or f.price_max is not None or f.category or f.city)
    return Parsed(filters=f, notes=describe(f, today), recognized=recognized)


def parse_conversation(user_messages: list[str], today: date) -> Parsed:
    """Накладывает друг на друга условия всех реплик: ответ на уточнение дополняет исходный запрос."""
    merged = SearchFilters(limit=8)
    recognized = False
    for message in user_messages:
        part = parse_query(message, today)
        recognized = recognized or part.recognized
        for name in ("city", "category", "date_from", "date_to", "price_max", "age", "kids"):
            value = getattr(part.filters, name)
            if value is not None:
                setattr(merged, name, value)
        if part.filters.free_only:
            merged.free_only, merged.price_max = True, None
        elif part.filters.price_max is not None:
            merged.free_only = False
    return Parsed(filters=merged, notes=describe(merged, today), recognized=recognized)
