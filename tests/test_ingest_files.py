import json
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from app.config import now_local
from app.ingest import ingest_records, load_records

START = (now_local() + timedelta(days=5)).replace(hour=19, minute=0, second=0, microsecond=0)


def test_json_import_with_duplicates_across_sources(session, tmp_path):
    path = tmp_path / "events.json"
    path.write_text(json.dumps([
        {"title": "Концерт «Осенний вальс»", "start_at": START.isoformat(), "source": "a.example", "price_min": 300},
        {"title": "Осенний вальс", "start_at": START.isoformat(), "source": "b.example", "price_max": 600},
        {"title": "Выставка кукол", "start_at": START.isoformat(), "source": "a.example", "category": "Выставка"},
    ], ensure_ascii=False), encoding="utf-8")
    assert ingest_records(session, load_records(path)) == {"created": 2, "merged": 1, "unchanged": 0}


def test_csv_import_handles_blanks_and_age_strings(session, tmp_path):
    path = tmp_path / "events.csv"
    path.write_text(
        "title,start_at,source,city,price_min,price_max,age_limit,description\n"
        f"Лекция об Уфе,{START.isoformat()},lib.example,в Уфе,0,0,12+,\n"
        f"Забег,{START.isoformat()},run.example,,,,,Для всей семьи\n",
        encoding="utf-8-sig",
    )
    stats = ingest_records(session, load_records(path))
    assert stats["created"] == 2
    from app.models import Event
    from sqlalchemy import select

    lecture = session.scalar(select(Event).where(Event.title == "Лекция об Уфе"))
    assert (lecture.city, lecture.age_limit, lecture.price_min) == ("Уфа", 12, 0)
    run = session.scalar(select(Event).where(Event.title == "Забег"))
    assert run.price_min is None and run.kids_friendly is False


def test_invalid_record_is_rejected(session):
    with pytest.raises(ValidationError):
        ingest_records(session, [{"title": "Без источника", "start_at": datetime(2026, 11, 8).isoformat()}])


def test_unknown_file_format_is_rejected(tmp_path):
    path = tmp_path / "events.xml"
    path.write_text("<x/>")
    with pytest.raises(SystemExit):
        load_records(path)
