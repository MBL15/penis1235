"""Нормализация текста, городов и категорий: основа для поиска и дедупликации."""
import re
from difflib import SequenceMatcher

CATEGORIES: dict[str, str] = {
    "theatre": "Театр",
    "concert": "Концерт",
    "exhibition": "Выставка",
    "festival": "Фестиваль",
    "kids": "Детям",
    "lecture": "Лекции и мастер-классы",
    "cinema": "Кино",
    "excursion": "Экскурсии",
    "sport": "Спорт",
    "other": "Другое",
}

# Корни слов, по которым категория угадывается из свободного текста (запрос, заголовок, CSV).
CATEGORY_STEMS: dict[str, tuple[str, ...]] = {
    "theatre": ("театр", "спектакл", "оперн", "опера", "оперы", "оперу", "оперет", "балет", "мюзикл"),
    "concert": ("концерт", "музык", "филармон", "джаз", "оркестр"),
    "exhibition": ("выставк", "музе", "галере", "экспозиц"),
    "festival": ("фестивал", "праздник", "сабантуй", "ярмарк"),
    "kids": ("детск", "детям", "малыш", "кукол", "семейн"),
    "lecture": ("лекци", "мастер", "воркшоп", "встреч"),
    "cinema": ("кино", "фильм", "киносеанс"),
    "excursion": ("экскурс", "прогулк"),
    "sport": ("спорт", "матч", "турнир", "забег", "марафон"),
}

# Категории, где период «с … по …» значит, что событие идёт всё это время (выставка открыта каждый день). Для остальных
# (театр, концерт, детские, лекции…) источники так записывают СЕРИЮ показов: известны первая и последняя даты, но не
# сами сеансы между ними. Выдавать серию за непрерывное событие нельзя: «в субботу» показало бы спектакль пятницы.
CONTINUOUS_CATEGORIES = ("exhibition", "excursion", "festival")

STATUSES: dict[str, str] = {
    "scheduled": "Состоится",
    "rescheduled": "Перенесено",
    "sold_out": "Билеты проданы",
    "cancelled": "Отменено",
}
STATUS_SEVERITY = {"scheduled": 0, "sold_out": 1, "rescheduled": 2, "cancelled": 3}
ACTIVE_STATUSES = ("scheduled", "rescheduled", "sold_out")

CITY_FORMS: dict[str, tuple[str, ...]] = {
    "Уфа": ("уфа", "уфе", "уфу", "уфы", "уфой", "уфимск"),
    "Стерлитамак": ("стерлитамак",),
    "Салават": ("салават",),
    "Нефтекамск": ("нефтекамск",),
    "Октябрьский": ("октябрьск",),
    "Сибай": ("сибай",),
    "Белорецк": ("белорецк",),
    "Кумертау": ("кумертау",),
    "Мелеуз": ("мелеуз",),
    "Ишимбай": ("ишимбай",),
    "Туймазы": ("туймаз",),
    "Бирск": ("бирск",),
    "Агидель": ("агидел",),
    "Учалы": ("учал",),
    "Баймак": ("баймак",),
    "Благовещенск": ("благовещенск",),
}

GENERIC_TITLE_WORDS = {
    "спектакль", "концерт", "мюзикл", "выставка", "фестиваль", "балет", "опера",
    "шоу", "программа", "вечер", "премьера", "представление", "проект", "театр",
    "в", "и", "на", "по", "для", "с", "из", "от", "к", "о", "а",
}

_NON_WORD = re.compile(r"[^0-9a-zа-я]+")


def norm_text(value: str) -> str:
    """Нижний регистр, ё→е, всё кроме букв и цифр → пробел."""
    return _NON_WORD.sub(" ", (value or "").lower().replace("ё", "е")).strip()


def tokens(value: str) -> list[str]:
    return norm_text(value).split()


def stem(token: str) -> str:
    """Грубая обрезка окончаний, чтобы «спектакли» находили «спектакль»."""
    if len(token) >= 6:
        return token[:-2]
    if len(token) >= 4:
        return token[:-1]
    return token


def title_key(title: str) -> str:
    """Заголовок без «спектакль/концерт/…», кавычек и служебных слов, для сравнения дублей."""
    return " ".join(t for t in tokens(title) if t not in GENERIC_TITLE_WORDS)


def _match_city(words: list[str]) -> str | None:
    """Сначала точные формы («уфа», «в салавате» → салават не точное), потом по началу слова.

    Иначе «проспект Салавата Юлаева, Уфа» дал бы Салават: слово улицы совпадает по началу раньше, чем названный город.
    """
    for word in words:
        for city, forms in CITY_FORMS.items():
            if word == norm_text(city) or word in forms:
                return city
    for word in words:
        for city, forms in CITY_FORMS.items():
            if any(word.startswith(form) for form in forms):
                return city
    return None


def canonical_city(value: str | None) -> str | None:
    """«в уфе», «Уфимск», «стерлитамак» → каноническое название; неизвестный город остаётся как есть."""
    if not value or not value.strip():
        return None
    return _match_city(tokens(value)) or value.strip()


def find_city_in_text(text: str) -> str | None:
    return _match_city(tokens(text))


# Месяцы похожи на названия городов («октябрь» ~ «Октябрьский»): к ним нечёткий поиск не применяется.
_MONTH_STEMS = ("январ", "феврал", "март", "апрел", "мая", "май", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр")
FUZZY_MIN_RATIO = 0.8


def find_city_fuzzy(text: str) -> tuple[str, str] | None:
    """Город, написанный с опечаткой: («Стерлитамак», «стерлитакмае»). None, если ничего похожего нет.

    Вызывать только для запросов пользователя после точного поиска: для адресов из источников нечёткое совпадение
    опасно. Короткие названия и формы (до 6 букв) исправлению не подлежат: слишком много случайных совпадений."""
    for word in tokens(text):
        if len(word) < 6 or word.startswith(_MONTH_STEMS) or word.isdigit():
            continue
        best: tuple[float, str] | None = None
        for city, forms in CITY_FORMS.items():
            for form in forms:
                if len(form) < 7:
                    continue
                ratio = SequenceMatcher(None, word[: len(form) + 1], form).ratio()
                if ratio >= FUZZY_MIN_RATIO and (best is None or ratio > best[0]):
                    best = (ratio, city)
        if best is not None:
            return best[1], word
    return None


def guess_category(text: str) -> str | None:
    words = tokens(text)
    for slug, stems in CATEGORY_STEMS.items():
        if any(w.startswith(s) for w in words for s in stems):
            return slug
    return None


def canonical_category(value: str | None, fallback_text: str = "") -> str:
    """Принимает slug, русское название или произвольный текст; в конце — «other»."""
    if value:
        low = value.strip().lower()
        if low in CATEGORIES:
            return low
        for slug, label in CATEGORIES.items():
            if low == label.lower():
                return slug
        guessed = guess_category(value)
        if guessed:
            return guessed
    return guess_category(fallback_text) or "other"


def parse_age(value) -> int | None:
    """12, "12+", "от 6 лет" → 12 / 12 / 6. Пусто или нет числа → None («не указан»); явное «0+» → 0."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    if isinstance(value, int):
        return max(value, 0)
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


def merge_age(a: int | None, b: int | None) -> int | None:
    """Возраст при слиянии двух записей: известное побеждает неизвестное, из двух известных берётся строже."""
    if a is None or b is None:
        return b if a is None else a
    return max(a, b)


_TAG_STEMS = (
    ("джаз", ("джаз",)), ("классическая музыка", ("симфон", "камерн", "органн", "классическ", "филармон")),
    ("опера", ("опер",)), ("балет", ("балет",)), ("стендап", ("стендап", "stand-up", "standup")), ("рок", ("рок-", "рок ", "панк", "метал")),
    ("фольклор", ("фолк", "народн", "курай")), ("мастер-класс", ("мастер-класс", "мастер класс")), ("квест", ("квест",)),
)


def derive_tags(title: str, description: str, *, kids: bool, free: bool, language: str | None) -> list[str]:
    """Метки события из названия и описания: только то, что названо явно («джаз» нет в тексте: метки нет)."""
    text = f"{title} {description or ''}".lower().replace("ё", "е")
    tags = [tag for tag, stems in _TAG_STEMS if any(stem in text for stem in stems)]
    if kids:
        tags.append("дети")
    if free:
        tags.append("бесплатно")
    if language == "ba":
        tags.append("башкирский язык")
    return tags


_SOURCE_TYPES = {"culture.ru": "api", "kudago.com": "api", "afisha.yandex.ru": "page", "kassir.ru": "page",
                 "admin": "manual", "manual": "manual"}


def source_type_for(source: str | None) -> str:
    """Тип первого источника: ручной ввод, импорт файла, API, страница или RSS (ТЗ §8.1)."""
    name = (source or "").strip().lower()
    if name in _SOURCE_TYPES:
        return _SOURCE_TYPES[name]
    if name.startswith(("admin", "manual", "moderator")):
        return "manual"
    if "rss" in name or name.endswith((".xml", ".atom")):
        return "rss"
    if "." in name:  # похоже на домен: сайт учреждения
        return "page"
    return "import"


KIDS_STEMS = ("детск", "детям", "детей", "дети", "детьм", "детиш", "ребен", "малыш", "кукол", "семейн")


def is_kids_text(text: str) -> bool:
    return any(w.startswith(KIDS_STEMS) for w in tokens(text))


def build_search_text(*parts: str) -> str:
    return " ".join(p for p in (norm_text(x) for x in parts) if p)


# ---------- язык и длительность (ТЗ §8.1): только то, что названо или следует из данных, без догадок ----------

LANGUAGES: dict[str, str] = {
    "ba": "башкирский", "ru": "русский", "tt": "татарский", "en": "английский", "cv": "чувашский", "mhr": "марийский",
}
_LANG_STEMS = (("башкирск", "ba"), ("татарск", "tt"), ("русск", "ru"), ("английск", "en"), ("чувашск", "cv"), ("марийск", "mhr"))
_LANG_RE = re.compile(r"\bна\s+(башкирск|татарск|русск|английск|чувашск|марийск)\w*\s+язык\w*")


def detect_language(text: str | None) -> str | None:
    """Язык мероприятия, только если он назван явно: «спектакль на башкирском языке». Иначе None: язык не угадываем."""
    match = _LANG_RE.search((text or "").lower().replace("ё", "е"))
    return dict(_LANG_STEMS)[match.group(1)] if match else None


def canonical_language(value) -> str | None:
    """Код языка из кода («ba») или названия («башкирский», «на башкирском»); неизвестное значение даёт None."""
    if not value:
        return None
    low = str(value).strip().lower().replace("ё", "е")
    if low in LANGUAGES:
        return low
    return next((code for prefix, code in _LANG_STEMS if low.startswith(prefix) or f" {prefix}" in f" {low}"), None)


def derive_duration(start, end) -> int | None:
    """Длительность в минутах, если известно время окончания в тот же день и время начала указано (00:00 значит «не указано»)."""
    if start is None or end is None or start.date() != end.date() or (start.hour, start.minute) == (0, 0) or end <= start:
        return None
    minutes = int((end - start).total_seconds() // 60)
    return minutes if 5 <= minutes <= 16 * 60 else None
