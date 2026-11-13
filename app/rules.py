"""Разбор запроса на русском без ИИ: запасной режим, когда нет ключа Gemini или он недоступен.

Главный принцип: чего не поняли или не умеем проверять, о том говорим прямо (Parsed.unsupported), а не молчим."""
import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .formatting import MONTHS_GEN, WEEKDAYS_FULL
from .normalize import CATEGORIES, CATEGORY_STEMS, KIDS_STEMS, find_city_fuzzy, find_city_in_text, norm_text
from .search import SearchFilters, resolve_when

WEEKDAY_STEMS = ("понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень")
MONTH_MAP = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
FAMILY_WORDS = KIDS_STEMS + ("сын", "доч", "внук", "племянн")
# «Джаз» или «балет» — это не просто «концерт» или «театр», а конкретная тема: слово идёт в поиск по тексту, а не теряется
# за категорией. Если пользователь назвал и общее слово («концерт джаза»), категория тоже учитывается.
KEYWORD_STEMS = ("джаз", "балет", "оперн", "опера", "оперы", "оперу", "оперет", "мюзикл", "симфон", "стендап", "кукол", "органн")
PEOPLE = {"двоих": 2, "двух": 2, "вдвоем": 2, "троих": 3, "трех": 3, "втроем": 3, "четверых": 4, "вчетвером": 4}

_PRICE_RE = re.compile(
    r"(?:до|дешевле|не дороже|не более чем|не более|максимум|в пределах)\s+(\d+(?:\s\d{3})*)(?:\s*(тысяч|тыс|к)\b)?"
)
# «2000 рублей на двоих»: бюджет на компанию, а фильтр цены работает по одному билету
_GROUP_PRICE_RE = re.compile(r"(\d+(?:\s\d{3})*)\s*(?:руб\w*|р)?\s*на\s+(" + "|".join(PEOPLE) + r")\b")
_AGE_RE = re.compile(r"(\d{1,2})\s*(?:лет|года|год|летн)")
_DATE_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(sorted(MONTH_MAP, key=len, reverse=True)) + r")\w*")
_CHEAP_RE = re.compile(r"\b(?:недорог\w*|дешев\w*|подешевле|бюджетн\w*)\b")
# Часы пишут с двоеточием («19:00»), поэтому разбираем их по исходному тексту: norm_text двоеточие убирает
_CLOCK_RE = re.compile(r"\b(?:после|не\s+раньше|не\s+позже|до|к|в|с)\s+\d{1,2}:\d{2}\b|\b(?:после|не\s+раньше|не\s+позже|до|к|в)\s+\d{1,2}\s*(?:час\w*|ч)\b")

# Условия, которые мы разобрать умеем, но проверить по базе не можем: у событий нет длительности, координат, языка и времени окончания.
_UNSUPPORTED = (
    ("время", re.compile(r"\b(?:утром|днем|вечером|ночью)\b")),
    ("время", _CLOCK_RE),
    # дорога раньше длительности: «ехать не больше 20 минут» — это расстояние, а не длительность события
    ("расстояние и дорога", re.compile(
        r"ехать\s+(?:не\s+)?(?:больше|более|дольше|до)?\s*\d+\s*мин\w*|добираться[^,.?!]*|\bв\s+центре\b|\bближе\s+к\s+\w+|\bрядом\b|\bпоближе\b"
        r"|\bнедалеко\b|\bблизко\b|\d+\s*км\b")),
    ("длительность", re.compile(
        r"(?:не\s+(?:дольше|более|больше|длиннее)|до)\s+(?:\d+|одного|двух|трех|четырех|пяти|получаса)\s*(?:час\w*|ч\b|мин\w*)")),
    ("язык мероприятия", re.compile(
        r"на\s+(?:башкирск|татарск|русск|английск|чувашск|марийск)\w*(?:\s+язык\w*)?|на\s+языке\s+\w+|по-башкирски")),
)


@dataclass
class Parsed:
    filters: SearchFilters
    notes: list[str] = field(default_factory=list)  # что мы поняли — показываем пользователю
    recognized: bool = False
    corrections: list[str] = field(default_factory=list)  # исправленные опечатки: «стерлитакмае» → Стерлитамак
    unsupported: list[str] = field(default_factory=list)  # условия, которые мы не смогли учесть: «время («после 19:00»)»
    price_label: str | None = None  # как назвать цену в ответе, если она пересчитана («500 ₽ за билет, 1000 ₽ на двоих»)


def _next_weekday(today: date, target: int) -> date:
    return today + timedelta(days=(target - today.weekday()) % 7)


def _day_label(d: date, today: date) -> str:
    full = f"{WEEKDAYS_FULL[d.weekday()]}, {d.day} {MONTHS_GEN[d.month - 1]}"
    if d == today:
        return f"сегодня, {full}"
    if d == today + timedelta(days=1):
        return f"завтра, {full}"
    return full


def describe(f: SearchFilters, today: date, price_label: str | None = None) -> list[str]:
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
        notes.append(price_label or f"до {f.price_max} ₽")
    if f.sort == "price":
        notes.append("сначала дешёвые")
    if f.city:
        notes.append(f.city)
    if f.q:
        notes.append(f"«{f.q}»")
    if f.category:
        notes.append(" или ".join(CATEGORIES.get(c, c).lower() for c in f.category.split(",")))
    return notes


def find_unsupported(text: str) -> list[str]:
    """Условия из запроса, которые мы не умеем проверять: время суток и окончания, длительность, расстояние, язык."""
    raw = (text or "").lower().replace("ё", "е")
    found: list[str] = []
    taken: list[tuple[int, int]] = []  # уже названные фрагменты: один и тот же кусок текста не считаем дважды
    for label, pattern in _UNSUPPORTED:
        for match in pattern.finditer(raw):
            if any(match.start() < end and start < match.end() for start, end in taken):
                continue
            taken.append(match.span())
            item = f"{label} («{match.group(0).strip()}»)"
            if item not in found:
                found.append(item)
    return found


def parse_query(text: str, today: date) -> Parsed:
    raw = (text or "").lower().replace("ё", "е")
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
    elif "конца месяца" in low or "этом месяце" in low or "до конца месяц" in low:
        f.date_from, f.date_to = today, today.replace(day=calendar.monthrange(today.year, today.month)[1])
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
    price_label = None
    no_clock = norm_text(_CLOCK_RE.sub(" ", raw))  # «до 20:00» — это время, а не цена в 20 рублей
    group = _GROUP_PRICE_RE.search(no_clock)
    if any(w.startswith(("бесплатн", "даром", "free")) for w in words) or "без оплаты" in low:
        f.free_only = True
    elif group:
        total, people = int(group.group(1).replace(" ", "")), PEOPLE[group.group(2)]
        f.price_max = total // people
        price_label = f"до {f.price_max} ₽ за билет ({total} ₽ на {group.group(2)})"
    else:
        price = _PRICE_RE.search(no_clock)
        # «до 5 лет» — это возраст, а не цена
        if price and not _AGE_RE.match(no_clock[price.start(1):]):
            amount = int(price.group(1).replace(" ", ""))
            f.price_max = amount * 1000 if price.group(2) else amount
    if _CHEAP_RE.search(raw):
        f.sort = "price"  # «недорого» без суммы: показываем сначала дешёвые

    # --- где и что ---
    corrections: list[str] = []
    f.city = find_city_in_text(text)
    if f.city is None:
        fuzzy = find_city_fuzzy(text)
        if fuzzy:
            f.city = fuzzy[0]
            corrections.append(f"«{fuzzy[1]}» → {fuzzy[0]}")
    keywords = [w for w in words if w.startswith(KEYWORD_STEMS)]
    if keywords:
        f.q = " ".join(keywords[:3])
    generic = [w for w in words if w not in keywords]
    for slug, stems in CATEGORY_STEMS.items():
        if slug != "kids" and any(w.startswith(stems) for w in generic):
            f.category = slug
            break

    recognized = bool(f.date_from or f.kids or f.free_only or f.price_max is not None or f.category or f.city or f.q)
    return Parsed(
        filters=f, notes=describe(f, today, price_label), recognized=recognized, corrections=corrections,
        unsupported=find_unsupported(text), price_label=price_label,
    )


def parse_conversation(user_messages: list[str], today: date) -> Parsed:
    """Накладывает друг на друга условия всех реплик: ответ на уточнение дополняет исходный запрос."""
    merged = SearchFilters(limit=8)
    recognized = False
    corrections: list[str] = []
    unsupported: list[str] = []
    price_label = None
    for message in user_messages:
        part = parse_query(message, today)
        recognized = recognized or part.recognized
        for name in ("city", "category", "q", "date_from", "date_to", "price_max", "age", "kids"):
            value = getattr(part.filters, name)
            if value is not None:
                setattr(merged, name, value)
        if part.filters.free_only:
            merged.free_only, merged.price_max, price_label = True, None, None
        elif part.filters.price_max is not None:
            merged.free_only, price_label = False, part.price_label
        if part.filters.sort == "price":
            merged.sort = "price"
        corrections += [c for c in part.corrections if c not in corrections]
        unsupported += [u for u in part.unsupported if u not in unsupported]
    return Parsed(
        filters=merged, notes=describe(merged, today, price_label), recognized=recognized,
        corrections=corrections, unsupported=unsupported, price_label=price_label,
    )
