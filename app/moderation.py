"""Правки модератора: редактирование карточки и публикация. Каждая правка пишется в журнал от имени модератора."""
from sqlalchemy.orm import Session

from . import audit
from .ingest import DEFAULT_CITY, _search_text
from .dedup import make_dedup_key
from .models import Event
from .normalize import canonical_category, canonical_city, canonical_language, norm_text, parse_age
from .schemas import EventPatch
from .venues import venue_key_for


class EditError(ValueError):
    """Правка не прошла проверку; текст понятен человеку."""


def edit_event(session: Session, event: Event, patch: EventPatch, actor: str) -> Event:
    """Применяет только переданные поля. Заведомо неверные сочетания (конец раньше начала, цена «до» меньше цены «от»)
    отклоняются, а не исправляются молча: модератор должен увидеть свою ошибку."""
    values = patch.model_dump(exclude_unset=True)
    if not values:
        raise EditError("Не передано ни одного поля для изменения")
    before = audit.snapshot(event)

    for field, value in values.items():
        if field in ("title", "city") and (value is None or not str(value).strip()):
            raise EditError(f"Поле {field} нельзя очистить")
        if field == "title":
            event.title = value.strip()
        elif field == "city":
            event.city = canonical_city(value) or value.strip() or DEFAULT_CITY
        elif field == "category":
            category = canonical_category(value)
            if value and category == "other" and norm_text(value) not in ("other", "другое"):
                raise EditError(f"Неизвестная категория: {value}")
            event.category = category
        elif field == "age_limit":
            event.age_limit = parse_age(value)
        elif field == "language":
            event.language = canonical_language(value) if value else None
            if value and event.language is None:
                raise EditError(f"Неизвестный язык: {value}. Допустимы коды ba, ru, tt, en, cv, mhr или названия")
        elif field in ("description", "venue", "address", "url", "image_url"):
            setattr(event, field, (value or "").strip() if field != "description" else (value or ""))
        else:
            setattr(event, field, value)

    if event.end_at is not None and event.end_at < event.start_at:
        raise EditError("Время окончания раньше времени начала")
    if event.price_min is not None and event.price_max is not None and event.price_max < event.price_min:
        raise EditError("Цена «до» меньше цены «от»")
    event.dedup_key = make_dedup_key(event.city, event.start_at, event.title)
    event.search_text = _search_text(event)
    event.venue_key = venue_key_for(event.venue)
    changes = audit.diff(before, audit.snapshot(event))
    if changes:
        audit.record(session, actor, "edit", event.id, changes)
    session.commit()
    return event


def set_publication(session: Session, event: Event, publication: str, actor: str) -> Event:
    if event.publication != publication:
        before = audit.snapshot(event)
        event.publication = publication
        audit.record(session, actor, "publication", event.id, audit.diff(before, audit.snapshot(event)))
        session.commit()
    return event
