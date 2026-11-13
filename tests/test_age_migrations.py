"""Возраст «не указан» против «0+» и миграции схемы (Alembic)."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text

from app import db
from app.maintenance import find_unknown_ages, fix_ages
from app.models import Event
from app.normalize import merge_age, parse_age
from app.search import SearchFilters, search

from .conftest import NOW


def test_unknown_age_is_none_and_explicit_zero_is_kept():
    assert parse_age(None) is None and parse_age("") is None and parse_age("без ограничений") is None
    assert parse_age("0+") == 0 and parse_age(0) == 0 and parse_age("от 6 лет") == 6 and parse_age("12+") == 12
    assert merge_age(None, None) is None and merge_age(None, 6) == 6 and merge_age(12, None) == 12
    assert merge_age(6, 12) == 12  # из двух известных берётся строже


def test_age_filter_does_not_treat_unknown_age_as_suitable_for_a_child(session, add):
    start = NOW + timedelta(days=1, hours=3)
    add("Спектакль «Макбет»", start, category="theatre", age_limit=None)
    add("Кукольный спектакль «Три медведи»", start, category="kids", age_limit=None, kids_friendly=True)
    add("Сказка «Репка»", start, category="kids", age_limit=0)
    add("Триллер «Ночь»", start, category="theatre", age_limit=16)
    titles = [e.title for e in search(session, SearchFilters(age=8), NOW)[0]]
    assert "Спектакль «Макбет»" not in titles and "Триллер «Ночь»" not in titles  # неизвестный и строгий возраст не подходят
    assert "Кукольный спектакль «Три медведи»" in titles and "Сказка «Репка»" in titles


def test_card_says_age_is_unknown_instead_of_showing_nothing(session, add):
    from app.formatting import event_to_out, render_event

    event, _ = add("Спектакль «Макбет»", NOW + timedelta(days=1), age_limit=None)
    assert event_to_out(event).age_limit is None
    assert "возраст не указан" in render_event(event)
    event.age_limit = 0
    assert "0+" in render_event(event)


def test_maintenance_turns_old_zeros_into_unknown_but_keeps_explicit_zero_plus(session, add):
    start = NOW + timedelta(days=1)
    unknown, _ = add("Концерт без возраста", start, age_limit=0)
    explicit, _ = add("Детская сказка 0+", start, age_limit=0)
    assert [e.id for e in find_unknown_ages(session)] == [unknown.id]
    fix_ages(session, apply=True)
    assert session.get(Event, unknown.id).age_limit is None and session.get(Event, explicit.id).age_limit == 0


@pytest.fixture
def file_engine(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'afisha.db'}")
    monkeypatch.setattr(db, "engine", engine)
    return engine


def _version(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def test_new_database_is_created_from_models_and_marked_with_the_latest_version(file_engine):
    db.init_db()
    assert _version(file_engine) == "0004"
    columns = {c["name"]: c for c in inspect(file_engine).get_columns("events")}
    assert columns["age_limit"]["nullable"] is True
    db.init_db()  # повторный запуск ничего не ломает
    assert _version(file_engine) == "0004"


def test_database_from_before_migrations_is_upgraded_and_keeps_its_data(file_engine):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path

    db.init_db()
    root = Path(db.__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    with file_engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0001")  # возвращаем схему «до миграций»: age_limit NOT NULL DEFAULT 0
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text(
            "INSERT INTO events (title, description, category, city, venue, address, start_at, age_limit, kids_friendly, url, image_url, "
            "status, publication, sources, dedup_key, search_text, created_at, updated_at) "
            "VALUES ('Старое событие', '', 'concert', 'Уфа', '', '', '2026-12-01 19:00:00', 0, 0, '', '', 'scheduled', 'published', "
            "'[]', 'k', 'старое', '2026-11-01 10:00:00', '2026-11-01 10:00:00')"))
    legacy = {c["name"]: c for c in inspect(file_engine).get_columns("events")}
    assert legacy["age_limit"]["nullable"] is False

    db.init_db()
    assert _version(file_engine) == "0004"
    with file_engine.connect() as conn:
        assert conn.execute(text("SELECT title, age_limit FROM events")).all() == [("Старое событие", None)]  # ноль стал «не указан»
