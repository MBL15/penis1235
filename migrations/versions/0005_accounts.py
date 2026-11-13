"""Аккаунты и роли (docs/08): users, user_sessions, visit_history, saved_searches, api_keys; автор у подписок и обращений,
ответ модератора на обращение.

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _tables() -> list[tuple[str, list, list[tuple[str, list[str], bool]]]]:
    """(таблица, столбцы, индексы). Описание отдельно, чтобы миграция создавала только то, чего ещё нет."""
    return [
        ("users", [
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_id", sa.BigInteger(), nullable=False),
            sa.Column("display_name", sa.String(100), nullable=False, server_default=""),
            sa.Column("role", sa.String(12), nullable=False, server_default="user"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime()),
            sa.Column("history_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("consent_at", sa.DateTime()),
        ], [("ix_users_telegram_id", ["telegram_id"], True), ("ix_users_role", ["role"], False)]),
        ("user_sessions", [
            sa.Column("token_hash", sa.String(64), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
        ], [("ix_user_sessions_user_id", ["user_id"], False), ("ix_user_sessions_expires_at", ["expires_at"], False)]),
        ("visit_history", [
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("event_id", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(8), nullable=False, server_default="view"),
            sa.Column("viewed_at", sa.DateTime(), nullable=False),
        ], [("ix_visit_history_user_id", ["user_id"], False), ("ix_visit_history_event_id", ["event_id"], False),
            ("ix_visit_history_viewed_at", ["viewed_at"], False)]),
        ("saved_searches", [
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("filters", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        ], [("ix_saved_searches_user_id", ["user_id"], False)]),
        ("api_keys", [
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("key_hash", sa.String(64), nullable=False),
            sa.Column("key_prefix", sa.String(12), nullable=False, server_default=""),
            sa.Column("name", sa.String(80), nullable=False, server_default=""),
            sa.Column("owner_user_id", sa.Integer()),
            sa.Column("rate_limit", sa.Integer(), nullable=False, server_default="600"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime()),
            sa.Column("last_used_at", sa.DateTime()),
            sa.Column("requests_total", sa.Integer(), nullable=False, server_default="0"),
        ], [("ix_api_keys_key_hash", ["key_hash"], True), ("ix_api_keys_owner_user_id", ["owner_user_id"], False)]),
    ]


# (таблица, столбец, тип, индекс)
_COLUMNS = [
    ("subscriptions", "user_id", sa.Integer(), True),
    ("feedback", "user_id", sa.Integer(), True),
    ("feedback", "reply", sa.Text(), False),
    ("feedback", "replied_at", sa.DateTime(), False),
    ("feedback", "notified_at", sa.DateTime(), False),
]


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for name, columns, indexes in _tables():
        if inspector.has_table(name):
            continue  # таблицу уже создал create_all при старте приложения
        op.create_table(name, *columns, **({"sqlite_autoincrement": True} if name == "users" else {}))
        for index_name, index_columns, unique in indexes:
            op.create_index(index_name, name, index_columns, unique=unique)
    inspector = sa.inspect(op.get_bind())
    for table, column, kind, indexed in _COLUMNS:
        if not inspector.has_table(table):
            continue
        if column not in {c["name"] for c in inspector.get_columns(table)}:
            op.add_column(table, sa.Column(column, kind, nullable=True))
        if indexed and f"ix_{table}_{column}" not in {i["name"] for i in inspector.get_indexes(table)}:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table, column, _, indexed in reversed(_COLUMNS):
        if inspector.has_table(table) and column in {c["name"] for c in inspector.get_columns(table)}:
            with op.batch_alter_table(table) as batch:
                if indexed:
                    batch.drop_index(f"ix_{table}_{column}")
                batch.drop_column(column)
    for name, _, _ in reversed(_tables()):
        if inspector.has_table(name):
            op.drop_table(name)
