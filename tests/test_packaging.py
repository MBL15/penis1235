"""Запуск «с нуля» (ТЗ §10): образ собирается без каталога wheels/, секреты не попадают в образ, а набор тестовых данных
реальный, корректный и грузится повторно без дублей."""
import json
import re
from datetime import datetime
from pathlib import Path

from app.ingest import ingest_records
from app.models import Event

ROOT = Path(__file__).resolve().parent.parent


def test_default_dockerfile_does_not_need_prebuilt_wheels():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "wheels" not in re.sub(r"(?m)^\s*#.*$", "", dockerfile)  # комментарии про офлайн-режим не считаются
    assert "pip install --no-cache-dir -r requirements.txt" in dockerfile
    assert "USER 1000" in dockerfile
    assert "wheels" in (ROOT / "deploy" / "Dockerfile.offline").read_text(encoding="utf-8")  # серверная сборка не потеряна


def test_compose_runs_the_site_and_keeps_the_bot_optional():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"(?m)^  web:", text) and re.search(r"(?m)^  bot:", text)
    assert 'profiles: ["bot"]' in text  # без токена обычный запуск не должен падать из-за бота
    assert "required: false" in text  # .env необязателен
    assert "read_only: true" in text and "cap_drop" in text


def test_secrets_and_databases_stay_out_of_the_image():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert {".env", "*.db", ".git"} <= set(ignored)
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert {".env", "afisha.db"} <= set(gitignore) and "!.env.example" in gitignore


def test_no_invented_events_ship_inside_the_application_package():
    """ТЗ §13: выдуманных мероприятий быть не должно. Генератор демо-данных живёт в tests/ и в образ не попадает."""
    assert not (ROOT / "app" / "seed.py").exists()
    offenders = [p.name for p in (ROOT / "app").rglob("*.py") if "demo-afisha" in p.read_text(encoding="utf-8")]
    assert not offenders, offenders


def test_reset_demo_removes_only_demo_events(session, add):
    from app.maintenance import reset_demo

    add("Выдуманное", datetime(2026, 11, 10, 18, 0), source="demo-afisha")
    add("Настоящее", datetime(2026, 11, 10, 19, 0), source="culture.ru")
    assert reset_demo(session) == 1
    assert [e.title for e in session.query(Event)] == ["Настоящее"]


def test_sample_data_is_valid_real_and_loads_without_duplicates(session):
    path = ROOT / "data" / "sample_events.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    assert len(records) >= 100
    assert not [r for r in records if str(r["source"]).startswith("demo")]  # выдуманных событий в наборе нет
    assert all(r["url"].startswith("https://") for r in records)  # у каждой записи есть ссылка на источник
    ingest_records(session, records)
    first = session.query(Event).count()
    assert 100 <= first <= len(records)
    stats = ingest_records(session, records)
    assert stats["created"] == 0 and session.query(Event).count() == first  # повторная загрузка не создаёт дублей
