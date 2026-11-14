"""Проверки ответов агента: условия запроса против карточек, честность формулировок, утечки и выдуманные названия.

Всё здесь работает только с БД и текстом ответа и не знает, как агент устроен внутри: одни и те же проверки применяются
к режиму правил, к модели и к поиску по ключевым словам."""
import re
from datetime import date, datetime, time, timedelta

from app.formatting import is_free
from app.models import Event
from app.normalize import norm_text
from app.search import SearchFilters

_NO_DATA = re.compile(
    r"ничего\s+(?:не\s+нашл\w+|нет|не\s+наш\w+)|не\s+нашл\w+|не\s+наш[её]л\w*|нет\s+(?:данных|событий|подходящих)|"
    r"не\s+найден\w*|отсутству\w+|в\s+афише\s+нет|событий\s+нет|"
    # «сейчас нет в нашей афише», «в Туймазах нет выставки динозавров», «пока нет балетных спектаклей» (прогон 13.11.2026):
    # отказ честный, но прежний словарь его не узнавал
    r"(?:сейчас|пока|к\s+сожалению,?)\s+(?:\w+\s+){0,6}?нет\b|нет\s+в\s+(?:нашей\s+)?(?:афише|базе)|"
    r"\bв\s+(?!источник)[а-яё-]+\s+нет\s+\w+"  # «в Туймазах нет выставки», но не «в источнике нет описания»
)
_RELAXED = re.compile(r"ослаб\w+|без\s+этого\s+слова|без\s+ограничени|даже\s+без")
_QUOTED = re.compile(r"«([^»]{3,120})»")


def says_no_data(reply: str) -> bool:
    return bool(_NO_DATA.search((reply or "").lower()))


def says_relaxed(reply: str) -> bool:
    return bool(_RELAXED.search((reply or "").lower()))


def mentions_cancellation(reply: str) -> bool:
    return "отмен" in (reply or "").lower()


def _clock(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _known_time(event: Event) -> bool:
    return event.start_at.time() != time(0, 0)  # 00:00 в данных значит «время не указано»


def violations(event: Event, c: dict) -> list[str]:
    """Какие условия запроса событие нарушает. Условия задаются в queries.json и читаются без участия поиска агента."""
    out: list[str] = []
    if c.get("city") and event.city != c["city"]:
        out.append(f"город {event.city}, нужен {c['city']}")
    if c.get("category") and event.category != c["category"]:
        out.append(f"категория {event.category}, нужна {c['category']}")
    if c.get("date"):
        first, last = (date.fromisoformat(x) for x in c["date"])
        end = event.end_at or event.start_at
        if not (end.date() >= first and event.start_at.date() <= last):
            out.append(f"дата {event.start_at:%d.%m}, нужен период {first:%d.%m}–{last:%d.%m}")
    if c.get("time_from") and not (_known_time(event) and event.start_at.time() >= _clock(c["time_from"])):
        out.append(f"начало {event.start_at:%H:%M}, нужно не раньше {c['time_from']}")
    if c.get("time_to") and not (_known_time(event) and event.start_at.time() <= _clock(c["time_to"])):
        out.append(f"начало {event.start_at:%H:%M}, нужно не позже {c['time_to']}")
    if c.get("ends_by"):
        end = event.start_at + timedelta(minutes=event.duration_min) if (event.duration_min and _known_time(event)) else event.end_at
        if end is None or end.date() != event.start_at.date() or end.time() > _clock(c["ends_by"]):
            out.append(f"окончание не подтверждено до {c['ends_by']}")
    if c.get("max_duration") and not (event.duration_min and event.duration_min <= c["max_duration"]):
        out.append(f"длительность {event.duration_min}, нужна не больше {c['max_duration']}")
    if c.get("price_max") is not None and not (event.price_min is not None and event.price_min <= c["price_max"]):
        out.append(f"цена от {event.price_min}, нужна не выше {c['price_max']}")
    if c.get("free") and not is_free(event):
        out.append("не бесплатное")
    if c.get("kids") and not event.kids_friendly:
        out.append("не детское")
    if c.get("age") is not None and (event.age_limit or 0) > c["age"]:
        out.append(f"возраст {event.age_limit}+, нужно не старше {c['age']}")
    if c.get("language") and event.language != c["language"]:
        out.append(f"язык {event.language or 'не указан'}, нужен {c['language']}")
    if c.get("venue") and norm_text(c["venue"]) not in norm_text(event.venue or ""):
        out.append(f"площадка {event.venue}, нужна {c['venue']}")
    if c.get("q"):
        stem = norm_text(c["q"])
        stem = stem[: max(4, len(stem) - 2)]
        if stem not in norm_text(f"{event.title} {event.description or ''} {event.venue or ''}"):
            out.append(f"нет слова «{c['q']}»")
    return out


def oracle_filters(raw: dict) -> SearchFilters:
    """Ожидаемая выдача: явные фильтры поиска из queries.json (даты строкой ГГГГ-ММ-ДД, время строкой ЧЧ:ММ)."""
    data = dict(raw)
    for key in ("date_from", "date_to"):
        if data.get(key):
            data[key] = date.fromisoformat(data[key])
    for key in ("time_from", "time_to", "ends_by"):
        if data.get(key):
            data[key] = _clock(data[key])
    data.setdefault("limit", 100)
    return SearchFilters(**data)


_ECHO = re.compile(r"\([^)]*\)|слов\w*\s+«[^»]*»")  # пересказ условий в скобках и «со словом «…»»: это слова запроса, а не названия


def invented_titles(reply: str, db_titles: list[str], user_text: str, own_phrases: str = "") -> list[str]:
    """Названия в «ёлочках», которых нет ни в базе, ни в словах самого пользователя, ни в готовых фразах сервиса
    (примеры запросов в подсказках, названия кнопок): признак выдуманного события. Пересказ условий запроса не проверяется."""
    titles = [norm_text(t) for t in db_titles if len(t) >= 4]
    user, own = norm_text(user_text), norm_text(own_phrases)
    invented = []
    for quoted in _QUOTED.findall(_ECHO.sub(" ", reply or "")):
        n = norm_text(quoted)
        if n in user or n in own or any(n in t or t in n for t in titles):
            continue
        invented.append(quoted)
    return invented


def without_service_quotes(reply: str, own_phrases: str) -> str:
    """Ответ без примеров запросов из готовых подсказок сервиса («до 1000 рублей» в совете): это не утверждения о событиях."""
    own = norm_text(own_phrases)
    return _QUOTED.sub(lambda m: " " if norm_text(m.group(1)) in own else m.group(0), reply or "")


def count_claims(reply: str) -> int:
    """Проверяемые утверждения в ответе: числа и время (две цифры и больше) и названия в «ёлочках»."""
    numbers = {t for t in re.findall(r"\d+(?::\d{2})?", reply or "") if len(t) >= 2}
    return len(numbers) + len(set(_QUOTED.findall(reply or "")))


def leaked(reply: str, prompt_text: str, size: int = 7) -> bool:
    """Повторяет ли ответ дословно кусок системного промпта (подряд `size` слов)."""
    words = norm_text(prompt_text).split()
    haystack = " " + " ".join(norm_text(reply or "").split()) + " "
    return any(" " + " ".join(words[i: i + size]) + " " in haystack for i in range(0, max(0, len(words) - size + 1)))
