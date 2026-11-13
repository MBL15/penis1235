"""Нормализация текста, городов и категорий: основа для поиска и дедупликации."""
import re

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


def parse_age(value) -> int:
    """12, "12+", "от 6 лет" → 12 / 12 / 6. Пусто → 0."""
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return max(value, 0)
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else 0


KIDS_STEMS = ("детск", "детям", "детей", "дети", "ребен", "малыш", "кукол", "семейн")


def is_kids_text(text: str) -> bool:
    return any(w.startswith(KIDS_STEMS) for w in tokens(text))


def build_search_text(*parts: str) -> str:
    return " ".join(p for p in (norm_text(x) for x in parts) if p)
