"""Поля схемы §8.1 (tags, source_type, source_hash, verified_at, confidence) и идемпотентность по хешу записи источника."""
import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.formatting import event_to_out
from app.models import Event
from app.normalize import derive_tags, source_type_for
from app.pipeline import process_batch

from .conftest import NOW

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_events.json"


def test_tags_are_taken_only_from_what_the_text_says():
    assert derive_tags("Вечер романсов", "", kids=False, free=False, language=None) == []  # в тексте нет ничего из словаря: метки нет
    assert derive_tags("Джазовый вечер", "", kids=False, free=False, language=None) == ["джаз"]
    tags = derive_tags("Кукольный спектакль", "на башкирском языке", kids=True, free=True, language="ba")
    assert tags == ["дети", "бесплатно", "башкирский язык"]
    assert derive_tags("Стендап-вечер", "", kids=False, free=False, language=None) == ["стендап"]


def test_source_type_by_name():
    assert source_type_for("culture.ru") == "api" and source_type_for("afisha.yandex.ru") == "page"
    assert source_type_for("admin:ivanov") == "manual" and source_type_for("rss.example.ru") == "rss"
    assert source_type_for("example.ru") == "page" and source_type_for("file") == "import" and source_type_for(None) == "import"


def test_new_event_gets_the_fields_and_the_api_shows_them(session, add):
    event, _ = add("Джаз-вечер в филармонии", NOW + timedelta(days=2), city="Уфа", price_min=0)
    row = session.get(Event, event.id)
    assert row.normalized_title and row.source_hash and row.source_type and row.tags is not None
    out = event_to_out(row)
    assert out.tags == list(row.tags) and out.source_type == row.source_type and out.verified_at is None and out.confidence is None


def test_importing_the_same_file_again_changes_nothing_and_creates_no_duplicates(session):
    records = json.loads(SAMPLE.read_text(encoding="utf-8"))[:100]  # файл на 100 событий: порог ТЗ §9.1 «без ручной правки и без дублей»
    first = process_batch(session, records)
    assert not first.rejected and first.created + first.updated == 100 and first.created > 70
    total = session.scalar(select(func.count()).select_from(Event))
    second = process_batch(session, records)
    third = process_batch(session, records)
    assert second.created == 0 and third.created == 0 and session.scalar(select(func.count()).select_from(Event)) == total
    assert third.updated == 0 and third.unchanged == 100  # после одного прохода повторная загрузка ничего не меняет
