"""Приём событий: нормализация → поиск дубля → создание или слияние.

CLI:  python -m app.ingest events.json|events.csv
"""
import argparse
import csv
import json
import sys
from functools import reduce
from pathlib import Path

from sqlalchemy.orm import Session

from .dedup import find_duplicate, make_dedup_key, worse_status
from .models import Event
from .normalize import (
    CATEGORIES,
    build_search_text,
    canonical_category,
    canonical_city,
    find_city_in_text,
    is_kids_text,
    parse_age,
)
from .schemas import EventIn

DEFAULT_CITY = "Уфа"


def _search_text(event: Event) -> str:
    return build_search_text(
        event.title, event.description, event.venue, event.city, event.address,
        CATEGORIES.get(event.category, ""), "детям" if event.kids_friendly else "",
    )


def _merge(event: Event, data: EventIn, *, category: str, age: int, kids: bool, same_source: bool) -> bool:
    """Обновляет событие данными из нового источника. Возвращает True, если что-то изменилось."""
    changed = False

    def put(field: str, value) -> None:
        nonlocal changed
        if getattr(event, field) != value:
            setattr(event, field, value)
            changed = True

    def fill(field: str, value) -> None:
        """Берём значение нового источника, если своего нет (или источник тот же и пришла свежая версия)."""
        if value in (None, ""):
            return
        if same_source or getattr(event, field) in (None, ""):
            put(field, value)

    if same_source:
        put("title", data.title)
    fill("venue", data.venue)
    fill("address", data.address)
    fill("url", data.url)
    fill("image_url", data.image_url)
    fill("end_at", data.end_at)

    if data.description and (same_source or len(data.description) > len(event.description or "")):
        put("description", data.description)

    if data.price_min is not None:
        if same_source or event.price_min is None:
            put("price_min", data.price_min)
            put("price_max", data.price_max)
        else:  # источники расходятся — показываем честный диапазон
            put("price_min", min(event.price_min, data.price_min))
            highs = [p for p in (event.price_max, data.price_max) if p is not None]
            put("price_max", max(highs) if highs else None)

    put("age_limit", age if same_source else max(event.age_limit or 0, age))
    put("kids_friendly", kids if same_source else (bool(event.kids_friendly) or kids))

    if event.category == "other" and category != "other":
        put("category", category)

    if same_source:
        put("start_at", data.start_at)
    elif (event.start_at.hour, event.start_at.minute) == (0, 0) and (data.start_at.hour, data.start_at.minute) != (0, 0):
        put("start_at", data.start_at)

    # Статус храним по каждому источнику, а у события он «самый тревожный» из них: свежая выгрузка источника A
    # не должна стирать отмену, о которой сообщил B. Записи без статуса (старые) считаем согласными с текущим статусом.
    sources = [{**s, "status": s.get("status", event.status)} for s in event.sources or []]
    entry = next((s for s in sources if s.get("name") == data.source), None)
    if entry is None:
        sources.append({"name": data.source, "url": data.source_url or data.url, "status": data.status})
    else:
        entry["status"] = data.status
    put("sources", sources)
    put("status", reduce(worse_status, (s["status"] for s in sources)))
    if not same_source:
        changed = True
    return changed


def upsert_event(session: Session, data: EventIn) -> tuple[Event, str]:
    """Возвращает (событие, 'created' | 'merged' | 'unchanged')."""
    event, outcome, _ = upsert_detailed(session, data)
    return event, outcome


def upsert_detailed(session: Session, data: EventIn) -> tuple[Event, str, bool]:
    """Как upsert_event, плюс признак «другой источник подтвердил уже известное событие»: так видно, сколько дублей между
    источниками нашла дедупликация (а не просто обновила свою же запись)."""
    city = canonical_city(data.city) or find_city_in_text(data.address) or DEFAULT_CITY
    category = canonical_category(data.category, f"{data.title} {data.description}")
    age = parse_age(data.age_limit)
    kids = data.kids_friendly if data.kids_friendly is not None else (
        category == "kids" or is_kids_text(f"{data.title} {data.description}")
    )

    existing = find_duplicate(
        session, city=city, start_at=data.start_at, title=data.title, venue=data.venue, url=data.url
    )
    if existing is None:
        event = Event(
            title=data.title.strip(),
            description=data.description,
            category=category,
            city=city,
            venue=data.venue,
            address=data.address,
            start_at=data.start_at,
            end_at=data.end_at,
            price_min=data.price_min,
            price_max=data.price_max,
            age_limit=age,
            kids_friendly=kids,
            url=data.url,
            image_url=data.image_url,
            status=data.status,
            sources=[{"name": data.source, "url": data.source_url or data.url, "status": data.status}],
        )
        event.dedup_key = make_dedup_key(city, event.start_at, event.title)
        event.search_text = _search_text(event)
        session.add(event)
        session.commit()
        return event, "created", False

    same_source = any(s.get("name") == data.source for s in existing.sources or [])
    changed = _merge(existing, data, category=category, age=age, kids=kids, same_source=same_source)
    if changed:
        existing.dedup_key = make_dedup_key(existing.city, existing.start_at, existing.title)
        existing.search_text = _search_text(existing)
        session.commit()
        return existing, "merged", not same_source
    return existing, "unchanged", False


def merge_events(session: Session, keep: Event, drop: Event) -> Event:
    """Сливает две уже сохранённые записи об одном событии в keep, drop удаляется. Нужна для ручной и пакетной чистки дублей
    (обычная склейка происходит при приёме, но источники могли прийти в другом порядке или с другими названиями)."""
    sources = [{**s, "status": s.get("status", keep.status)} for s in keep.sources or []]
    for src in drop.sources or []:
        src = {**src, "status": src.get("status", drop.status)}
        entry = next((x for x in sources if x.get("name") == src.get("name")), None)
        if entry is None:
            sources.append(src)
        elif worse_status(entry["status"], src["status"]) == src["status"]:
            entry["status"] = src["status"]
    keep.sources = sources
    keep.status = reduce(worse_status, (x["status"] for x in sources), keep.status)
    for field in ("venue", "address", "url", "image_url", "end_at"):
        if not getattr(keep, field) and getattr(drop, field):
            setattr(keep, field, getattr(drop, field))
    if len(drop.description or "") > len(keep.description or ""):
        keep.description = drop.description
    if keep.price_min is None and drop.price_min is not None:
        keep.price_min, keep.price_max = drop.price_min, drop.price_max
    elif keep.price_min is not None and drop.price_min is not None:
        keep.price_min = min(keep.price_min, drop.price_min)
        highs = [p for p in (keep.price_max, drop.price_max) if p is not None]
        keep.price_max = max(highs) if highs else None
    keep.age_limit = max(keep.age_limit or 0, drop.age_limit or 0)
    keep.kids_friendly = bool(keep.kids_friendly or drop.kids_friendly)
    if keep.category == "other" and drop.category != "other":
        keep.category = drop.category
    if (keep.start_at.hour, keep.start_at.minute) == (0, 0) and (drop.start_at.hour, drop.start_at.minute) != (0, 0)             and drop.start_at.date() == keep.start_at.date():
        keep.start_at = drop.start_at
    keep.dedup_key = make_dedup_key(keep.city, keep.start_at, keep.title)
    keep.search_text = _search_text(keep)
    session.delete(drop)
    session.commit()
    return keep


def load_records(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else data["events"]
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as fh:
            # Пустые ячейки выкидываем, чтобы сработали значения по умолчанию.
            return [{k: v for k, v in row.items() if v not in ("", None)} for row in csv.DictReader(fh)]
    raise SystemExit(f"Неизвестный формат файла: {path.suffix} (поддерживаются .json и .csv)")


def ingest_records(session: Session, records: list[dict]) -> dict[str, int]:
    stats = {"created": 0, "merged": 0, "unchanged": 0}
    for record in records:
        _, outcome = upsert_event(session, EventIn.model_validate(record))
        stats[outcome] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Загрузить события из JSON/CSV с дедупликацией")
    parser.add_argument("file", type=Path)
    args = parser.parse_args(argv)

    from .db import SessionLocal, init_db

    init_db()
    with SessionLocal() as session:
        stats = ingest_records(session, load_records(args.file))
    print(f"Создано: {stats['created']}, слито с существующими: {stats['merged']}, без изменений: {stats['unchanged']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
