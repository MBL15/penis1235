"""Окружение Alembic: схема берётся из app.models, адрес базы из app.config (DATABASE_URL)."""
from alembic import context
from sqlalchemy import create_engine, pool

from app import config
from app.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")  # движок приложения передаёт своё соединение
    if connection is None:
        connectable = create_engine(config.DATABASE_URL, poolclass=pool.NullPool)
        with connectable.connect() as connection:
            _run(connection)
    else:
        _run(connection)


def _run(connection) -> None:
    # render_as_batch: SQLite не умеет менять столбцы, Alembic пересоздаёт таблицу сам
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Автономный режим (--sql) не поддерживается: схему меняют и данные переносят в одной транзакции")
run_migrations_online()
