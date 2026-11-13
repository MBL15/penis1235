"""Поля схемы события из ТЗ §8.1: normalized_title, tags, source_type, source_hash, verified_at, confidence, review_reason.

Столбцы необязательные. Заполняются при старте приложения (app/db.py: backfill_defaults) и при проверке карточек
(app/verify.py). Миграция безопасна для базы, где столбцы уже добавил add_missing_columns.

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

COLUMNS = (
    ("normalized_title", sa.String(300)), ("tags", sa.JSON()), ("source_type", sa.String(12)), ("source_hash", sa.String(40)),
    ("verified_at", sa.DateTime()), ("confidence", sa.Float()), ("review_reason", sa.String(300)),
)


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("events")}
    for name, kind in COLUMNS:
        if name not in existing:
            op.add_column("events", sa.Column(name, kind, nullable=True))
    indexes = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("events")}
    if "ix_events_normalized_title" not in indexes:
        op.create_index("ix_events_normalized_title", "events", ["normalized_title"])


def downgrade() -> None:
    op.drop_index("ix_events_normalized_title", table_name="events")
    with op.batch_alter_table("events") as batch:
        for name, _ in COLUMNS:
            batch.drop_column(name)
