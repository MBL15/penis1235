"""Выгружает небольшой набор реальных событий из рабочей базы в data/sample_events.json.

Нужен для запуска «с нуля» (docker compose up): набор подгружается командой python -m app.ingest и показывает всё, что
умеет сервис, на настоящих афишах, а не на выдуманных данных. Запуск из корня проекта:
    python scripts/export_sample.py [--size 120]
События, подтверждённые двумя источниками, выгружаются двумя записями: при загрузке видно склейку дублей.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.config import now_local  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.formatting import is_demo, is_past  # noqa: E402
from app.models import Event  # noqa: E402

OUT = ROOT / "data" / "sample_events.json"


def records(event: Event) -> list[dict]:
    base = {
        "title": event.title,
        "description": event.description or "",
        "category": event.category,
        "city": event.city,
        "venue": event.venue or "",
        "address": event.address or "",
        "start_at": event.start_at.isoformat(),
        "end_at": event.end_at.isoformat() if event.end_at else None,
        "series_until": event.series_until.isoformat() if event.series_until else None,
        "price_min": event.price_min,
        "price_max": event.price_max,
        "age_limit": event.age_limit or None,
        "kids_friendly": bool(event.kids_friendly),
        "image_url": event.image_url or "",
        "status": event.status,
    }
    out = []
    for source in event.sources or [{"name": "unknown", "url": event.url}]:
        row = {**base, "source": source["name"], "source_url": source.get("url") or event.url or "", "url": source.get("url") or event.url or ""}
        out.append({k: v for k, v in row.items() if v not in (None, "")})
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=120)
    size = parser.parse_args().size
    now = now_local()
    with SessionLocal() as session:
        events = [e for e in session.scalars(select(Event).order_by(Event.start_at, Event.id)) if not is_demo(e) and not is_past(e, now)]
    two_sources = [e for e in events if len(e.sources or []) >= 2][: max(size // 8, 1)]
    chosen = {e.id: e for e in two_sources}
    for e in (e for e in events if e.city != "Уфа"):  # малые города нужны для примеров из ТЗ (Стерлитамак, Салават)
        if len(chosen) >= size // 3 + len(two_sources):
            break
        chosen.setdefault(e.id, e)
    rest = [e for e in events if e.id not in chosen]
    step = max(len(rest) // max(size - len(chosen), 1), 1)
    for e in rest[::step]:
        if len(chosen) >= size:
            break
        chosen[e.id] = e
    rows = [row for e in sorted(chosen.values(), key=lambda e: (e.start_at, e.id)) for row in records(e)]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Событий: {len(chosen)}, записей: {len(rows)} → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
