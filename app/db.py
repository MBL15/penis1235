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
    from .normalize import norm_text

    # Обычный SQL, а не ORM: заполнение служебных столбцов не должно менять updated_at у всех событий.
    with bind.begin() as conn:
        changed = conn.execute(text("UPDATE events SET publication = 'published' WHERE publication IS NULL")).rowcount or 0
        rows = conn.execute(text("SELECT id, venue FROM events WHERE venue_key IS NULL")).all()
        if rows:
            conn.execute(text("UPDATE events SET venue_key = :key WHERE id = :id"),
                         [{"id": row.id, "key": norm_text(row.venue or "")} for row in rows])
        changed += len(rows)
    return changed


def init_db() -> None:
    Base.metadata.create_all(engine)
    for name in add_missing_columns(engine):
        log.info("В базу добавлен столбец %s", name)
    filled = backfill_defaults(engine)
    if filled:
        log.info("Заполнены значения новых столбцов у %d событий", filled)


def get_session() -> Iterator[Session]:
    """FastAPI-зависимость: одна сессия на запрос."""
    with SessionLocal() as session:
        yield session
