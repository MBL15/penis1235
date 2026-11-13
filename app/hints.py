"""Подсказка к пустой выдаче (ТЗ §6.1: «что не найдено и какое ограничение можно ослабить»).

Одна функция для API, сайта и бота. Условия снимаются по одному: называется то, без которого что-то находится, и сколько
именно. Агент (agent.py) использует собственную, более подробную логику, эта нужна обычному поиску и фильтрам."""
from dataclasses import replace
from datetime import datetime

from .models import Event
from .normalize import CATEGORIES
from .search import SearchFilters, search
from .config import now_local

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")


def _date_text(value) -> str:
    return f"{value.day} {MONTHS[value.month - 1]}"


def describe_constraints(f: SearchFilters) -> list[str]:
    """Условия поиска словами: «город Уфа», «до 1000 ₽», «бесплатно»…"""
    parts = []
    if f.q:
        parts.append(f"слово «{f.q}»")
    if f.city:
        parts.append(f"город {f.city}")
    if f.venue:
        parts.append(f"площадка «{f.venue}»")
    if f.category:
        parts.append("категория " + ", ".join(CATEGORIES.get(c, c).lower() for c in f.category.split(",")))
    if f.date_from and f.date_to:
        parts.append(f"{_date_text(f.date_from)}" if f.date_from == f.date_to else f"с {_date_text(f.date_from)} по {_date_text(f.date_to)}")
    elif f.date_from:
        parts.append(f"с {_date_text(f.date_from)}")
    if f.time_from or f.time_to:
        parts.append("время " + " ".join(x for x in (f"после {f.time_from:%H:%M}" if f.time_from else "", f"до {f.time_to:%H:%M}" if f.time_to else "") if x))
    if f.ends_by:
        parts.append(f"закончится до {f.ends_by:%H:%M}")
    if f.max_duration:
        parts.append(f"не дольше {f.max_duration} мин")
    if f.language:
        parts.append(f"язык {f.language}")
    if f.free_only:
        parts.append("бесплатно")
    elif f.price_max is not None:
        parts.append(f"до {f.price_max} ₽")
    if f.age is not None:
        parts.append(f"возраст {f.age}")
    if f.kids:
        parts.append("детям")
    return parts


# (название условия для подсказки, как его снять). Сначала то, что проще всего ослабить.
def _relaxations(f: SearchFilters):
    if f.price_max is not None or f.free_only:
        yield "цене", replace(f, price_max=None, free_only=False)
    if f.age is not None or f.kids:
        yield "возрасту", replace(f, age=None, kids=None)
    if f.ends_by or f.max_duration:
        yield "длительности", replace(f, ends_by=None, max_duration=None)
    if f.time_from or f.time_to:
        yield "времени начала", replace(f, time_from=None, time_to=None)
    if f.language:
        yield "языку", replace(f, language=None)
    if f.q:
        yield "слову из запроса", replace(f, q=None)
    if f.venue:
        yield "площадке", replace(f, venue=None)
    if f.date_from or f.date_to:
        yield "дате", replace(f, date_from=None, date_to=None)
    if f.category:
        yield "категории", replace(f, category=None)
    if f.city:
        yield "городу", replace(f, city=None)


def empty_hint(session, f: SearchFilters, now: datetime | None = None) -> str:
    """Текст для пустой выдачи: что искали и без какого условия что-то находится. Ничего не меняет в фильтрах вызывающего."""
    now = now or now_local()
    base = replace(f, limit=1, offset=0, exclude_ids=[])
    constraints = describe_constraints(f)
    asked = f"Искали: {', '.join(constraints)}. " if constraints else ""
    for name, relaxed in _relaxations(base):
        events, total = search(session, relaxed, now)
        if total:
            nearest = events[0]
            extra = f" Ближайшее: «{nearest.title[:60]}», {_date_text(nearest.start_at)}." if name in ("дате", "городу") else ""
            return f"{asked}Ничего нет. Без условия по {name} нашлось {total}.{extra} Попробуйте убрать это условие."
    return f"{asked}Подходящих событий в афише пока нет. Загляните позже: данные обновляются."
