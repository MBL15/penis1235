"""Представление событий: цена, дата, статус, карточка для чата/бота и сериализация в API."""
import html as _html
from datetime import datetime, timedelta

from .config import now_local
from .models import Event
from .normalize import CATEGORIES, LANGUAGES, STATUSES
from .schemas import EventOut, SourceRef

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
# Событие без времени окончания считаем идущим ещё час после начала.
ONGOING_GRACE = timedelta(hours=1)
MAX_DESCRIPTION = 1500  # в карточке для чата: сообщение Telegram вмещает 4096 символов вместе с остальными строками


def is_free(event: Event) -> bool:
    return event.price_min == 0 and (event.price_max is None or event.price_max == 0)


def price_text(event: Event) -> str:
    lo, hi = event.price_min, event.price_max
    if lo is None and hi is None:
        return "Цена не указана"
    if is_free(event):
        return "Бесплатно"
    if lo is None:
        return f"до {hi} ₽"
    if hi is None:
        return f"от {lo} ₽"
    if hi == lo:
        return f"{lo} ₽"
    return f"{lo}–{hi} ₽"


def duration_text(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    return f"{hours} ч {rest} мин" if hours and rest else (f"{hours} ч" if hours else f"{rest} мин")


def is_series(event: Event) -> bool:
    """Серия показов: известна дата одного показа и конец серии, остальные даты неизвестны."""
    return event.series_until is not None and event.series_until.date() > event.start_at.date()


def is_past(event: Event, now: datetime | None = None) -> bool:
    now = now or now_local()
    if is_series(event):
        return event.series_until < now
    if event.end_at is not None:
        return event.end_at < now
    return event.start_at < now - ONGOING_GRACE


def when_text(event: Event, now: datetime | None = None) -> str:
    """«сб, 8 ноября, 18:30»; для многодневных: «1 ноября – 20 ноября», а если уже идёт, то «до 20 ноября»."""
    start, end = event.start_at, event.end_at
    if end is not None and end.date() > start.date():
        if start <= (now or now_local()):
            return f"до {end.day} {MONTHS_GEN[end.month - 1]}"
        return f"{start.day} {MONTHS_GEN[start.month - 1]} – {end.day} {MONTHS_GEN[end.month - 1]}"
    day = f"{WEEKDAYS[start.weekday()]}, {start.day} {MONTHS_GEN[start.month - 1]}"
    has_time = (start.hour, start.minute) != (0, 0)
    single = f"{day}, {start:%H:%M}" if has_time else day
    if is_series(event):
        # Даты между известным показом и концом серии источник не сообщает: так и пишем, ничего не выдумываем.
        last = event.series_until
        year = f" {last.year}" if last.year != (now or now_local()).year else ""
        until = f"показы до {last.day} {MONTHS_GEN[last.month - 1]}{year}"
        return until if start <= (now or now_local()) else f"{single} · {until}"
    return single


def is_demo(event: Event) -> bool:
    sources = event.sources or []
    return bool(sources) and all(str(s.get("name", "")).startswith("demo") for s in sources)


def event_to_out(event: Event, now: datetime | None = None) -> EventOut:
    sources = [SourceRef(**s) for s in (event.sources or [])]
    return EventOut(
        id=event.id,
        title=event.title,
        description=event.description or "",
        category=event.category,
        category_label=CATEGORIES.get(event.category, CATEGORIES["other"]),
        city=event.city,
        venue=event.venue or "",
        address=event.address or "",
        start_at=event.start_at,
        end_at=event.end_at,
        series_until=event.series_until,
        duration_min=event.duration_min,
        language=event.language,
        language_label=LANGUAGES.get(event.language or "", ""),
        lat=event.lat,
        lon=event.lon,
        price_min=event.price_min,
        price_max=event.price_max,
        price_text=price_text(event),
        is_free=is_free(event),
        age_limit=event.age_limit or 0,
        kids_friendly=bool(event.kids_friendly),
        url=event.url or "",
        image_url=event.image_url or "",
        status=event.status,
        status_label=STATUSES.get(event.status, event.status),
        publication=event.publication or "published",
        is_past=is_past(event, now),
        sources=sources,
        source_count=len(sources),
        is_demo=is_demo(event),
    )


def render_event(event: Event, *, html: bool = False, details: bool = False) -> str:
    """Карточка события текстом. html=True — для Telegram (parse_mode=HTML), значения экранируются."""
    esc = _html.escape if html else (lambda s: s)
    title = esc(event.title)
    lines = [f"<b>{title}</b>" if html else title]
    lines.append(f"📅 {esc(when_text(event))}")
    place = ", ".join(p for p in (event.venue, event.city) if p)
    if details and event.address:
        place = f"{place} ({event.address})" if place else event.address
    if place:
        lines.append(f"📍 {esc(place)}")
    meta = [price_text(event)]
    if event.age_limit:
        meta.append(f"{event.age_limit}+")
    if event.status != "scheduled":
        meta.append(STATUSES.get(event.status, event.status))
    lines.append("💳 " + esc(" · ".join(meta)))
    if is_demo(event):
        lines.append("🧪 Демо-событие (выдумано для презентации)")
    if details and event.description:
        lines.append("")
        description = event.description
        if len(description) > MAX_DESCRIPTION:  # режем до экранирования: иначе можно разрезать «&amp;» пополам
            description = description[:MAX_DESCRIPTION].rsplit(" ", 1)[0] + "…"
        lines.append(esc(description))
    if details and event.sources:
        names = ", ".join(s.get("name", "") for s in event.sources)
        lines.append(esc(f"Источники: {names}"))
    return "\n".join(lines)


def render_list(events: list[Event], *, html: bool = False) -> str:
    return "\n\n".join(f"{i}. {render_event(e, html=html)}" for i, e in enumerate(events, 1))
