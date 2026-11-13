"""Приём событий: нормализация → поиск дубля → создание или слияние.

CLI:  python -m app.ingest events.json|events.csv
"""
import argparse
import csv
import io
import json
import sys
from functools import reduce
from pathlib import Path

from sqlalchemy.orm import Session

from . import audit

from .dedup import find_duplicate, make_dedup_key, worse_status
from .models import Event
from .normalize import (
    CATEGORIES,
    build_search_text,
    canonical_category,
    canonical_city,
    canonical_language,
    derive_duration,
    detect_language,
    find_city_in_text,
    is_kids_text,
    norm_text,
    parse_age,
)
from .schemas import EventIn

DEFAULT_CITY = "Уфа"


def _search_text(event: Event) -> str:
    return build_search_text(
        event.title, event.description, event.venue, event.city, event.address,
        CATEGORIES.get(event.category, ""), "детям" if event.kids_friendly else "",
    )


def _merge(event: Event, data: EventIn, *, category: str, age: int, kids: bool, same_source: bool, language: str | None = None) -> bool:
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
    fill("language", language)
    fill("lat", data.lat)
    fill("lon", data.lon)
    if data.series_until is not None:
        if same_source or event.series_until is None:
            put("series_until", data.series_until)
        # Раньше серия записывалась одним длинным интервалом: теперь это серия, интервал снимаем.
        if same_source and event.end_at is not None and event.end_at == data.series_until:
            put("end_at", None)

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

    put("duration_min", data.duration_min or derive_duration(event.start_at, event.end_at) or event.duration_min)

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


def upsert_event(session: Session, data: EventIn, *, actor: str = "system") -> tuple[Event, str]:
    """Возвращает (событие, 'created' | 'merged' | 'unchanged'). actor попадает в журнал изменений."""
    event, outcome, _ = upsert_detailed(session, data, actor=actor)
    return event, outcome


def upsert_detailed(session: Session, data: EventIn, *, actor: str = "system") -> tuple[Event, str, bool]:
    """Как upsert_event, плюс признак «другой источник подтвердил уже известное событие»: так видно, сколько дублей между
    источниками нашла дедупликация (а не просто обновила свою же запись). Каждое создание и изменение пишется в журнал."""
    city = canonical_city(data.city) or find_city_in_text(data.address) or DEFAULT_CITY
    category = canonical_category(data.category, f"{data.title} {data.description}")
    age = parse_age(data.age_limit)
    kids = data.kids_friendly if data.kids_friendly is not None else (
        category == "kids" or is_kids_text(f"{data.title} {data.description}")
    )

    language = canonical_language(data.language) or detect_language(f"{data.title}\n{data.description}")

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
            series_until=data.series_until,
            duration_min=data.duration_min or derive_duration(data.start_at, data.end_at),
            language=language,
            lat=data.lat,
            lon=data.lon,
            price_min=data.price_min,
            price_max=data.price_max,
            age_limit=age,
            kids_friendly=kids,
            url=data.url,
            image_url=data.image_url,
            status=data.status,
            publication=data.publication,
            venue_key=norm_text(data.venue),
            sources=[{"name": data.source, "url": data.source_url or data.url, "status": data.status}],
        )
        event.dedup_key = make_dedup_key(city, event.start_at, event.title)
        event.search_text = _search_text(event)
        session.add(event)
        session.flush()  # нужен номер события для журнала
        audit.record(session, actor, "create", event.id, {
            "title": event.title, "start_at": event.start_at.isoformat(), "city": event.city, "source": data.source,
            "publication": event.publication})
        session.commit()
        return event, "created", False

    same_source = any(s.get("name") == data.source for s in existing.sources or [])
    before = audit.snapshot(existing)
    changed = _merge(existing, data, category=category, age=age, kids=kids, same_source=same_source, language=language)
    if changed:
        existing.dedup_key = make_dedup_key(existing.city, existing.start_at, existing.title)
        existing.search_text = _search_text(existing)
        existing.venue_key = norm_text(existing.venue)
        audit.record(session, actor, "update" if same_source else "merge", existing.id,
                     {**audit.diff(before, audit.snapshot(existing)), "source": data.source})
        session.commit()
        return existing, "merged", not same_source
    return existing, "unchanged", False


def merge_events(session: Session, keep: Event, drop: Event, *, actor: str = "system") -> Event:
    """Сливает две уже сохранённые записи об одном событии в keep, drop удаляется. Нужна для ручной и пакетной чистки дублей
    (обычная склейка происходит при приёме, но источники могли прийти в другом порядке или с другими названиями)."""
    before = audit.snapshot(keep)
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
    for field in ("venue", "address", "url", "image_url", "end_at", "series_until"):
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
    keep.venue_key = norm_text(keep.venue)
    audit.record(session, actor, "merge", keep.id, {**audit.diff(before, audit.snapshot(keep)), "merged_from_event": drop.id})
    audit.record(session, actor, "delete", drop.id, {"title": drop.title, "merged_into": keep.id})
    session.delete(drop)
    session.commit()
    return keep


def parse_records(text: str, kind: str) -> list[dict]:
    """Текст файла (json или csv) → список записей. Ошибка формата: ValueError с понятным сообщением."""
    kind = kind.lower().lstrip(".")
    text = text.lstrip("\ufeff")
    if kind == "json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Файл не является корректным JSON: {exc.msg} (строка {exc.lineno})") from exc
        if isinstance(data, dict):
            data = data.get("events")
        if not isinstance(data, list):
            raise ValueError("В JSON ожидался список событий или объект с ключом events")
        return data
    if kind == "csv":
        # Пустые ячейки выкидываем, чтобы сработали значения по умолчанию.
        return [{k: v for k, v in row.items() if v not in ("", None)} for row in csv.DictReader(io.StringIO(text, newline=""))]
    raise ValueError(f"Неизвестный формат файла: .{kind} (поддерживаются .json и .csv)")


def load_records(path: Path) -> list[dict]:
    try:
        return parse_records(path.read_text(encoding="utf-8-sig"), path.suffix)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def ingest_records(session: Session, records: list[dict], *, actor: str = "import") -> dict[str, int]:
    """Загружает записи по одной: плохая запись отклоняется с причиной, остальные грузятся (ТЗ §6.1)."""
    from .pipeline import process_batch

    report = process_batch(session, records, actor=actor)
    return {"created": report.created, "merged": report.updated, "unchanged": report.unchanged, "rejected": len(report.rejected)}


def print_report(report, file=None) -> None:
    print(f"Создано: {report.created}, обновлено: {report.updated}, без изменений: {report.unchanged}, "
          f"отклонено: {len(report.rejected)}", file=file)
    for item in report.rejected[:50]:
        print(f"  запись {item.row} «{item.title}»: {'; '.join(item.reasons)}", file=file)
    if len(report.rejected) > 50:
        print(f"  … и ещё {len(report.rejected) - 50}", file=file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Загрузить события из JSON/CSV с дедупликацией")
    parser.add_argument("file", type=Path)
    parser.add_argument("--draft", action="store_true", help="новые события сохранить черновиками (их увидит только модератор)")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    from .db import SessionLocal, init_db
    from .pipeline import process_batch

    init_db()
    records = load_records(args.file)
    with SessionLocal() as session:
        report = process_batch(session, records, actor=f"import:{args.file.name}"[:100], publish=not args.draft,
                               first_row=2 if args.file.suffix.lower() == ".csv" else 1)
    print_report(report)
    return 1 if report.rejected and not (report.created or report.updated or report.unchanged) else 0


if __name__ == "__main__":
    sys.exit(main())
