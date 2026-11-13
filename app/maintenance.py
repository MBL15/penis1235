"""Служебные операции с базой.

  python -m app.maintenance series            # показать события, записанные как «длинный период», хотя это серии показов
  python -m app.maintenance series --apply    # исправить их (делайте копию базы перед этим)
"""
import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Event
from .normalize import CONTINUOUS_CATEGORIES

# Источники, которые записывали серию показов как один период «первая дата – последняя».
SERIES_AS_PERIOD_SOURCES = ("kassir.ru",)


def find_series_stored_as_period(session: Session) -> list[Event]:
    """События, у которых серия показов записана как непрерывный период (end_at позже дня начала).

    Выставки, экскурсии и фестивали идут непрерывно и не трогаются; так же не трогаются события источников,
    которые периодами серии не записывают."""
    found = []
    for event in session.scalars(select(Event).where(Event.end_at.is_not(None))):
        if event.end_at.date() <= event.start_at.date() or event.category in CONTINUOUS_CATEGORIES:
            continue
        if any(s.get("name") in SERIES_AS_PERIOD_SOURCES for s in event.sources or []):
            found.append(event)
    return found


def fix_series(session: Session, *, apply: bool = False) -> list[Event]:
    """Переносит конец периода в series_until. Возвращает затронутые события; без apply ничего не меняет."""
    events = find_series_stored_as_period(session)
    if apply:
        for event in events:
            event.series_until, event.end_at = event.end_at, None
        session.commit()
    return events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Служебные операции с базой")
    parser.add_argument("task", choices=["series"])
    parser.add_argument("--apply", action="store_true", help="записать изменения (без флага только показывает)")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    from .db import SessionLocal, init_db

    init_db()
    with SessionLocal() as session:
        events = fix_series(session, apply=args.apply)
    for event in events[:20]:
        print(f"#{event.id} {event.title[:60]!r}: {event.start_at:%Y-%m-%d} → серия до {(event.series_until or event.end_at):%Y-%m-%d}")
    verb = "исправлено" if args.apply else "найдено (запустите с --apply, чтобы исправить)"
    print(f"Серий показов, записанных как период: {verb} {len(events)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
