"""Разбор запроса на русском без ИИ: запасной режим, когда нет ключа Gemini или он недоступен.

Главный принцип: чего не поняли или не умеем проверять, о том говорим прямо (Parsed.unsupported), а не молчим."""
import calendar
import re
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from difflib import get_close_matches

from .formatting import MONTHS_GEN, WEEKDAYS_FULL, duration_text
from .normalize import (
    CATEGORIES, CATEGORY_STEMS, CITY_FORMS, KIDS_STEMS, canonical_language, find_city_fuzzy, find_city_in_text, norm_text,
)
from .safety import has_event_signal
from .search import SearchFilters, resolve_when

WEEKDAY_STEMS = ("понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень")
MONTH_MAP = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
FAMILY_WORDS = KIDS_STEMS + ("сын", "доч", "внук", "племянн", "семьи", "семье", "семьей", "семью")  # не «семь»: это число
# «Джаз» или «балет» — это не просто «концерт» или «театр», а конкретная тема: слово идёт в поиск по тексту, а не теряется
# за категорией. Если пользователь назвал и общее слово («концерт джаза»), категория тоже учитывается.
KEYWORD_STEMS = ("джаз", "балет", "оперн", "опера", "оперы", "оперу", "оперет", "мюзикл", "симфон", "стендап", "кукол", "органн")
PEOPLE = {"двоих": 2, "двух": 2, "вдвоем": 2, "троих": 3, "трех": 3, "втроем": 3, "четверых": 4, "вчетвером": 4}
LANG_PREPOSITIONAL = {"ba": "башкирском", "ru": "русском", "tt": "татарском", "en": "английском", "cv": "чувашском", "mhr": "марийском"}
DURATION_WORDS = {"одного": 1, "двух": 2, "трех": 3, "четырех": 4, "пяти": 5}

_PRICE_RE = re.compile(
    r"(?:до|дешевле|не дороже|не более чем|не более|максимум|в пределах)\s+(\d+(?:\s\d{3})*)(?:\s*(тысяч|тыс|к)\b)?"
)
# «2000 рублей на двоих»: бюджет на компанию, а фильтр цены работает по одному билету
_GROUP_PRICE_RE = re.compile(r"(\d+(?:\s\d{3})*)\s*(?:руб\w*|р)?\s*на\s+(" + "|".join(PEOPLE) + r")\b")
_AGE_RE = re.compile(r"(\d{1,2})\s*(?:лет|года|год|летн)")
_DATE_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(sorted(MONTH_MAP, key=len, reverse=True)) + r")\w*")
_CHEAP_RE = re.compile(r"\b(?:недорог\w*|дешев\w*|подешевле|бюджетн\w*)\b")

# Часы пишут с двоеточием («19:00») или словом «час», поэтому разбираем их по исходному тексту: norm_text двоеточие убирает
_CLOCK = r"(?:(\d{1,2}):(\d{2})|(\d{1,2})\s*(?:час\w*|ч\b))"
_ENDS_A = re.compile(r"(?:закончи\w*|заканч\w*|освободи\w*)[^.?!;]{0,40}?(?:не\s+позже|до|к)\s+" + _CLOCK)
_ENDS_B = re.compile(r"\b(?:не\s+позже|до)\s+" + _CLOCK + r"[^.?!;]{0,25}?(?:закончи\w*|заканч\w*)")
_AFTER = re.compile(r"\b(?:после|не\s+раньше|начиная\s+с|с)\s+" + _CLOCK)
_BEFORE = re.compile(r"\b(?:не\s+позже|до|к)\s+" + _CLOCK)
_AT = re.compile(r"\bв\s+" + _CLOCK)
_ANY_CLOCK = re.compile(r"\b(?:после|не\s+раньше|не\s+позже|до|к|в|с)\s+" + _CLOCK)
_DURATION_RE = re.compile(
    r"\b(?:не\s+(?:дольше|более|больше|длиннее)|до|максимум)\s+(?:(\d+)|(одного|двух|трех|четырех|пяти))\s*(час\w*|ч\b|мин\w*)"
    r"|\b(?:не\s+(?:дольше|более|больше)|до)\s+(получаса)")
_LANG_ASK_RE = re.compile(r"\bна\s+(башкирск|татарск|русск|английск|чувашск|марийск)\w*(?:\s+язык\w*)?")
# Дорога и расстояние: координат у событий нет, поэтому проверить такие условия мы не можем и говорим об этом прямо
_DISTANCE_RE = re.compile(
    r"ехать\s+(?:не\s+)?(?:больше|более|дольше|до)?\s*\d+\s*мин\w*|добираться[^,.?!]*|\bв\s+центре\b|\bближе\s+к\s+\w+|\bрядом\b|\bпоближе\b"
    r"|\bнедалеко\b|\bблизко\b|\d+\s*км\b")

# Слова, которые сами ничего не ищут: просьбы, вопросы, связки, общие названия «события» и «цены». Всё остальное, что правила не
# разобрали («Земфира», «цирк», «керлинг»), ищется как слово в названии и описании, а если не нашлось, об этом говорится прямо.
FILLER_STEMS = (
    "куда", "пойти", "пойд", "идти", "сходи", "посмотр", "послуш", "посовет", "порекоменд", "подскаж", "покаж", "найд", "хоч",
    "хотим", "хотел", "можно", "нужн", "надо", "когда", "каки", "каку", "котор", "мероприят", "событ", "афиш", "вариант",
    "ближайш", "нибуд", "пожалуйста", "также", "тоже", "друг", "рубл", "тысяч", "минут", "билет", "стоит", "сколько", "рядом",
    "город", "республик", "башкир", "башкорт", "сейчас", "сегодня", "завтра", "послезавтра", "выходн", "недел", "месяц",
    "вечер", "утром", "днем", "ночью", "дешев", "подешев", "недорог", "бюджет", "бесплатн", "дорог", "интересн", "весел",
    "хорош", "классн", "красив", "необычн", "любой", "любое", "любые", "любую", "времен", "начал", "закончи", "заканч",
    "длитель", "дольше", "дальше", "ближе", "ехать", "добира", "двоих", "троих", "вдвоем", "втроем", "семь", "компани",
    "развлеч", "отдохн", "заняться", "провести", "чтобы", "чтоб", "этот", "этом", "этих", "этой", "есть", "будет", "были",
    "была", "было", "быть", "очень", "самое", "самый", "самая", "лучш", "последн", "первое", "первый", "первая", "похож",
    "такое", "такой", "такие", "хотя", "либо", "если", "потом", "после", "перед", "через", "всего", "всех", "всем", "ещё", "еще",
    "что", "где", "как", "для", "про", "мне", "нам", "или", "ниже", "выше", "более", "менее", "пока", "уже",
    "дорож", "расскаж", "известн", "выполн", "написан", "описани", "соревнован", "скажи", "ответь", "интересу", "ищу", "искать",
    "поищи", "всей", "всю", "весь", "вся", "девушк", "парн", "друз", "подруг", "родител", "бабушк", "дедушк", "коллег", "супруг",
    "конца", "конец", "сделай", "план", "долг", "переезд", "поездк", "женой", "мужем", "состо", "проход", "пройд", "отмен", "перенес", "перенос", "пользовател",  # бот дописывает к вопросу служебную справку «(Город пользователя: …)»
)
# Слова категорий целиком: по ним исправляются опечатки («спектакь» → спектакль)
CATEGORY_WORDS = {
    "спектакль": "theatre", "театр": "theatre", "мюзикл": "theatre", "концерт": "concert", "филармония": "concert",
    "выставка": "exhibition", "экспозиция": "exhibition", "фестиваль": "festival", "лекция": "lecture", "воркшоп": "lecture",
    "экскурсия": "excursion", "прогулка": "excursion", "киносеанс": "cinema",
}
_WORD_VOCAB = tuple(CATEGORY_WORDS) + tuple(WEEKDAY_STEMS) + tuple(LANG_PREPOSITIONAL.values())


def _known_stems() -> tuple[str, ...]:
    stems = list(WEEKDAY_STEMS) + list(MONTH_MAP) + list(FAMILY_WORDS) + list(KEYWORD_STEMS) + list(FILLER_STEMS)
    stems += [s for group in CATEGORY_STEMS.values() for s in group]
    stems += [form for forms in CITY_FORMS.values() for form in forms]
    stems += ["башкирск", "татарск", "русск", "английск", "чувашск", "марийск", "язык", "бесплатн", "даром", "free"]
    return tuple(dict.fromkeys(stems))


KNOWN_STEMS = _known_stems()


def residual_words(text: str, skip: set[str] | None = None) -> list[str]:
    """Слова запроса, которых правила не поняли и которые не похожи на опечатку известного слова: «Земфира», «цирк», «керлинг»."""
    out = []
    for word in norm_text(text).split():
        if len(word) < 4 or word.isdigit() or word in (skip or ()) or word.startswith(KNOWN_STEMS):
            continue
        if get_close_matches(word, _WORD_VOCAB, n=1, cutoff=0.84):
            continue
        out.append(word)
    return out[:3]


@dataclass
class Parsed:
    filters: SearchFilters
    notes: list[str] = field(default_factory=list)  # что мы поняли — показываем пользователю
    recognized: bool = False
    corrections: list[str] = field(default_factory=list)  # исправленные опечатки: «стерлитакмае» → Стерлитамак
    unsupported: list[str] = field(default_factory=list)  # условия, которые мы не смогли учесть: «расстояние и дорога («ближе к центру»)»
    price_label: str | None = None  # как назвать цену в ответе, если она пересчитана («500 ₽ за билет, 1000 ₽ на двоих»)
    intent: bool = False  # есть ли намерение искать события (город сам по себе его не даёт)
    similar_to: int | None = None  # «а что-нибудь похожее»: номер события из прошлого ответа
    refinements: list[str] = field(default_factory=list)  # «другие варианты», «в другой день»
    guessed_q: bool = False  # слово для поиска угадано (правила его не поняли): при нехватке результатов отбрасывается первым


_MONTH_PREPOSITIONS = ("в", "на", "за", "до", "с", "по", "к")


def month_only(words: list[str]) -> int | None:
    """«В декабре», «на январь»: месяц без числа. «9 мая» сюда не попадает: там число, и это точная дата."""
    for i, word in enumerate(words):
        if i == 0 or words[i - 1] not in _MONTH_PREPOSITIONS:
            continue
        for stem, number in MONTH_MAP.items():
            if word.startswith(stem) and len(word) <= len(stem) + 3:
                return number
    return None


def month_range(month: int, today: date) -> tuple[date, date]:
    """Весь месяц (ближайший такой), для текущего — с сегодняшнего дня."""
    year = today.year if month >= today.month else today.year + 1
    first, last = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    return (today if first < today else first), last


def _next_weekday(today: date, target: int) -> date:
    return today + timedelta(days=(target - today.weekday()) % 7)


def _day_label(d: date, today: date) -> str:
    full = f"{WEEKDAYS_FULL[d.weekday()]}, {d.day} {MONTHS_GEN[d.month - 1]}"
    if d == today:
        return f"сегодня, {full}"
    if d == today + timedelta(days=1):
        return f"завтра, {full}"
    return full


def _hm(value: time) -> str:
    return value.strftime("%H:%M")


def describe(f: SearchFilters, today: date, price_label: str | None = None) -> list[str]:
    """Условия поиска человеческим языком: «суббота, 8 ноября», «до 1000 ₽», «для детей»."""
    notes: list[str] = []
    if f.date_from and f.date_to:
        if f.date_from == f.date_to:
            notes.append(_day_label(f.date_from, today))
        else:
            a, b = f.date_from, f.date_to
            notes.append(f"{a.day} {MONTHS_GEN[a.month - 1]} – {b.day} {MONTHS_GEN[b.month - 1]}")
    if f.time_from and f.time_to:
        notes.append(f"начало {_hm(f.time_from)}–{_hm(f.time_to)}")
    elif f.time_from:
        notes.append(f"начало после {_hm(f.time_from)}")
    elif f.time_to:
        notes.append(f"начало до {_hm(f.time_to)}")
    if f.ends_by:
        notes.append(f"закончится не позже {_hm(f.ends_by)}")
    if f.max_duration:
        notes.append(f"не дольше {duration_text(f.max_duration)}")
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
    if f.language:
        notes.append(f"на {LANG_PREPOSITIONAL.get(canonical_language(f.language) or '', f.language)} языке")
    if f.q:
        notes.append(f"«{f.q}»")
    if f.category:
        notes.append(" или ".join(CATEGORIES.get(c, c).lower() for c in f.category.split(",")))
    if f.exclude_ids:
        notes.append("без уже показанных")
    return notes


def find_unsupported(text: str) -> list[str]:
    """Условия, которые мы не умеем проверять: расстояние и время в пути (у событий нет координат пользователя)."""
    raw = (text or "").lower().replace("ё", "е")
    found: list[str] = []
    for match in _DISTANCE_RE.finditer(raw):
        item = f"расстояние и дорога («{match.group(0).strip()}»)"
        if item not in found:
            found.append(item)
    return found


def _clock(match: re.Match) -> time | None:
    hour, minute = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), "0")
    h, m = int(hour), int(minute)
    return time(h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


def _at(minutes: int) -> time:
    minutes = max(1, min(minutes, 23 * 60 + 59))
    return time(minutes // 60, minutes % 60)


def parse_times(raw: str) -> tuple[dict, list[tuple[int, int]]]:
    """Время в запросе → {time_from, time_to, ends_by} и занятые фрагменты текста.

    «закончилось не позже 20:00» — окончание, «после 19:00» — начало не раньше, «до 12:00» — начало не позже, «в 19:00» —
    начало в пределах получаса, «вечером» — начало после 17:00."""
    found: dict = {}
    taken: list[tuple[int, int]] = []

    def free(m: re.Match) -> bool:
        return not any(m.start() < end and start < m.end() for start, end in taken)

    for pattern, key in ((_ENDS_A, "ends_by"), (_ENDS_B, "ends_by"), (_AFTER, "time_from"), (_BEFORE, "time_to")):
        for m in pattern.finditer(raw):
            value = _clock(m)
            if value is not None and key not in found and free(m):
                found[key] = value
                taken.append(m.span())
    for m in _AT.finditer(raw):
        value = _clock(m)
        if value is not None and "time_from" not in found and "time_to" not in found and free(m):
            center = value.hour * 60 + value.minute
            found["time_from"], found["time_to"] = _at(center - 30), _at(center + 30)
            taken.append(m.span())
    if "time_from" not in found and "time_to" not in found:
        if re.search(r"\bвечером\b|\bпо\s+вечерам\b|\bк\s+вечеру\b", raw):
            found["time_from"] = time(17, 0)
        elif re.search(r"\bутром\b|\bс\s+утра\b", raw):
            found["time_to"] = time(12, 0)
        elif re.search(r"\bднем\b|\bднём\b", raw):
            found["time_from"], found["time_to"] = time(12, 0), time(17, 0)
        elif re.search(r"\bночью\b", raw):
            found["time_from"] = time(22, 0)
    return found, taken


def parse_duration(raw: str, skip: list[tuple[int, int]]) -> tuple[int | None, list[tuple[int, int]]]:
    """«не дольше двух часов», «до 90 минут», «до получаса» → минуты. Фразы про дорогу (skip) длительностью не считаются."""
    for m in _DURATION_RE.finditer(raw):
        if any(m.start() < end and start < m.end() for start, end in skip):
            continue
        if m.group(4):
            minutes = 30
        else:
            number = int(m.group(1)) if m.group(1) else DURATION_WORDS[m.group(2)]
            minutes = number if m.group(3).startswith("мин") else number * 60
        if 5 <= minutes <= 24 * 60:
            return minutes, [m.span()]
    return None, []


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
    elif month_only(words):
        f.date_from, f.date_to = month_range(month_only(words), today)
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

    # --- во сколько и на сколько ---
    distance_spans = [m.span() for m in _DISTANCE_RE.finditer(raw)]
    times, time_spans = parse_times(raw)
    f.time_from, f.time_to, f.ends_by = times.get("time_from"), times.get("time_to"), times.get("ends_by")
    f.max_duration, duration_spans = parse_duration(raw, distance_spans)
    asked_language = _LANG_ASK_RE.search(raw)
    if asked_language:
        f.language = canonical_language(asked_language.group(1))

    # --- с кем ---
    if any(w.startswith(FAMILY_WORDS) for w in words):
        f.kids = True
        age = _AGE_RE.search(low)
        if age:
            f.age = int(age.group(1))

    # --- сколько ---
    price_label = None
    cleaned = raw
    for start, end in sorted(time_spans + duration_spans + distance_spans, reverse=True):
        cleaned = cleaned[:start] + " " + cleaned[end:]  # «до 20:00» — время, «до 2 часов» — длительность, а не цена
    no_clock = norm_text(_ANY_CLOCK.sub(" ", cleaned))
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
    typed: set[str] = set()  # опечатки, которые уже исправлены: в «непонятые слова» они не попадают
    f.city = find_city_in_text(text)
    if f.city is None:
        fuzzy = find_city_fuzzy(text)
        if fuzzy:
            f.city = fuzzy[0]
            corrections.append(f"«{fuzzy[1]}» → {fuzzy[0]}")
            typed.update(norm_text(fuzzy[1]).split())
    keywords = [w for w in words if w.startswith(KEYWORD_STEMS)]
    if keywords:
        f.q = " ".join(keywords[:3])
    generic = [w for w in words if w not in keywords]
    for slug, stems in CATEGORY_STEMS.items():
        if slug != "kids" and any(w.startswith(stems) for w in generic):
            f.category = slug
            break
    if f.category is None:  # «спектакь» → спектакль: опечатка в названии категории
        for word in generic:
            hit = get_close_matches(word, list(CATEGORY_WORDS), n=1, cutoff=0.84) if len(word) >= 6 else []
            if hit:
                f.category = CATEGORY_WORDS[hit[0]]
                corrections.append(f"«{word}» → {hit[0]}")
                typed.add(word)
                break

    specific = bool(
        f.date_from or f.kids or f.free_only or f.price_max is not None or f.category or f.q or f.time_from or f.time_to
        or f.ends_by or f.max_duration or f.language
    )
    unknown = []
    if not f.q and (specific or f.city):
        # Незнакомое слово («Земфира», «цирк») не выбрасываем: ищем его в названиях и описаниях. Не нашлось — скажем об этом.
        unknown = residual_words(cleaned, typed)  # без времени, длительности и расстояния: они уже разобраны
        if unknown:
            f.q = " ".join(unknown)
    return Parsed(
        filters=f, notes=describe(f, today, price_label), recognized=specific or bool(f.city), corrections=corrections,
        unsupported=find_unsupported(text), price_label=price_label, intent=specific or bool(unknown) or has_event_signal(text),
        guessed_q=bool(unknown),
    )


_MORE_RE = re.compile(r"\b(?:еще|другие|другое|иные)\b")
_OTHER_DAY_RE = re.compile(r"\b(?:в|на)\s+друг\w+\s+ден\w*|\bдруг\w+\s+дат\w*|\bдруго\w+\s+дн\w*")
_SIMILAR_RE = re.compile(r"\bпохож\w*|\bв\s+том\s+же\s+духе\b|\bтак\w*\s+же\b")


def parse_conversation(user_messages: list[str], today: date, context_ids: list[int] | None = None) -> Parsed:
    """Накладывает друг на друга условия всех реплик: ответ на уточнение дополняет исходный запрос.

    context_ids: номера событий из прошлого ответа. Они нужны для уточнений «а ещё», «в другой день», «что-то похожее»."""
    merged = SearchFilters(limit=8)
    recognized = intent = False
    corrections: list[str] = []
    unsupported: list[str] = []
    price_label = None
    guessed_q = False
    for message in user_messages:
        part = parse_query(message, today)
        recognized, intent = recognized or part.recognized, intent or part.intent
        if part.filters.q is not None:
            guessed_q = part.guessed_q
        for name in ("city", "category", "q", "date_from", "date_to", "price_max", "age", "kids", "max_duration", "ends_by", "language"):
            value = getattr(part.filters, name)
            if value is not None:
                setattr(merged, name, value)
        if part.filters.time_from is not None or part.filters.time_to is not None:
            merged.time_from, merged.time_to = part.filters.time_from, part.filters.time_to  # новое время заменяет прежнее целиком
        if part.filters.free_only:
            merged.free_only, merged.price_max, price_label = True, None, None
        elif part.filters.price_max is not None:
            merged.free_only, price_label = False, part.price_label
        if part.filters.sort == "price":
            merged.sort = "price"
        corrections += [c for c in part.corrections if c not in corrections]
        unsupported += [u for u in part.unsupported if u not in unsupported]

    refinements: list[str] = []
    similar_to = None
    last = norm_text(user_messages[-1]) if user_messages else ""
    if context_ids:
        if _OTHER_DAY_RE.search(last):
            merged.date_from = merged.date_to = None
            merged.exclude_ids = list(context_ids)
            refinements.append("в другой день")
        elif _SIMILAR_RE.search(last):
            similar_to = context_ids[0]
            refinements.append("похожие на уже показанное")
        elif _MORE_RE.search(last):
            merged.exclude_ids = list(context_ids)
            refinements.append("другие варианты")
        elif _CHEAP_RE.search(last):
            # «А подешевле» без суммы: показать другие, подешевле. С суммой («до 1500») это уточнение условия, и уже показанные
            # события, которые подходят, должны остаться в выдаче.
            named = parse_query(user_messages[-1], today).filters
            if named.price_max is None and not named.free_only:
                merged.exclude_ids = list(context_ids)
    parsed_recognized = recognized or bool(refinements)
    return Parsed(
        filters=merged, notes=describe(merged, today, price_label), recognized=parsed_recognized, corrections=corrections,
        unsupported=unsupported, price_label=price_label, intent=intent or bool(refinements), similar_to=similar_to,
        refinements=refinements, guessed_q=guessed_q,
    )
