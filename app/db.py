import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import config
from .models import Base

log = logging.getLogger("afisha.db")

_connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def add_missing_columns(bind: Engine) -> list[str]:
    """Добавляет в уже существующие таблицы новые необязательные столбцы модели.

    create_all создаёт только отсутствующие таблицы, а столбцы в старой базе не появляются. Это временная замена
    миграциям (Alembic в планах): работает для столбцов, допускающих NULL. Возвращает имена добавленных столбцов.
    """
    added: list[str] = []
    inspector = inspect(bind)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            if not column.nullable:
                log.error("В таблице %s нет столбца %s, а он обязателен: добавьте его вручную", table.name, column.name)
                continue
            ddl = column.type.compile(dialect=bind.dialect)
            with bind.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}'))
            added.append(f"{table.name}.{column.name}")
    return added


def backfill_defaults(bind: Engine) -> int:
    """Заполняет значения новых столбцов в уже существующих строках: публикация и ключ площадки. Возвращает число строк."""
    from .venues import venue_key_for

    # Обычный SQL, а не ORM: заполнение служебных столбцов не должно менять updated_at у всех событий.
    with bind.begin() as conn:
        changed = conn.execute(text("UPDATE events SET publication = 'published' WHERE publication IS NULL")).rowcount or 0
        rows = conn.execute(text("SELECT id, venue FROM events WHERE venue_key IS NULL")).all()
        if rows:
            conn.execute(text("UPDATE events SET venue_key = :key WHERE id = :id"),
                         [{"id": row.id, "key": venue_key_for(row.venue)} for row in rows])
        changed += len(rows)
        changed += _backfill_derived(conn)
    return changed


def _backfill_derived(conn) -> int:
    """Длительность из окончания в тот же день и явно названный язык для строк, загруженных до появления этих столбцов."""
    from datetime import datetime

    from .normalize import derive_duration, detect_language

    columns = {c["name"] for c in inspect(conn).get_columns("events")}
    if not {"start_at", "end_at", "duration_min", "language", "title", "description"} <= columns:
        return 0  # самая старая схема: заполнять пока нечем

    def moment(value):
        return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))

    changed = 0
    durations = []
    for row in conn.execute(text("SELECT id, start_at, end_at FROM events WHERE duration_min IS NULL AND end_at IS NOT NULL")):
        minutes = derive_duration(moment(row.start_at), moment(row.end_at))
        if minutes is not None:
            durations.append({"id": row.id, "value": minutes})
    if durations:
        conn.execute(text("UPDATE events SET duration_min = :value WHERE id = :id"), durations)
        changed += len(durations)
    languages = []
    for row in conn.execute(text("SELECT id, title, description FROM events WHERE language IS NULL")):
        code = detect_language(f"{row.title}\n{row.description or ''}")
        if code:
            languages.append({"id": row.id, "value": code})
    if languages:
        conn.execute(text("UPDATE events SET language = :value WHERE id = :id"), languages)
        changed += len(languages)
    return changed


def run_migrations(bind: Engine, *, fresh: bool = False) -> str:
    """Приводит схему к последней версии (Alembic). Возвращает, что сделано: «created», «stamped» или «upgraded».

    Новая база (fresh) уже создана целиком из моделей и помечается последней версией. База, созданная до появления
    миграций, помечается исходной версией 0001 и мигрирует дальше. Повторный запуск ничего не ломает."""
    from alembic import command
    from alembic.config import Config

    from pathlib import Path

    inspector = inspect(bind)
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "migrations"))
    with bind.begin() as connection:
        cfg.attributes["connection"] = connection
        if fresh:
            command.stamp(cfg, "head")
            return "created"
        if not inspector.has_table("alembic_version"):
            command.stamp(cfg, "0001")
            result = "stamped"
        else:
            result = "upgraded"
        command.upgrade(cfg, "head")
        return result


def init_db() -> None:
    if engine.url.drivername.startswith("sqlite") and engine.url.database in (None, "", ":memory:"):
        Base.metadata.create_all(engine)  # тесты и временные базы: миграции не нужны
        return
    fresh = not inspect(engine).has_table("events")
    Base.metadata.create_all(engine)  # новая база целиком; в старой появятся только недостающие таблицы
    for name in add_missing_columns(engine):
        log.info("В базу добавлен столбец %s", name)
    log.info("Миграции схемы: %s", run_migrations(engine, fresh=fresh))
    filled = backfill_defaults(engine)
    if filled:
        log.info("Заполнены значения новых столбцов у %d событий", filled)


def get_session() -> Iterator[Session]:
    """FastAPI-зависимость: одна сессия на запрос."""
    with SessionLocal() as session:
        yield session
