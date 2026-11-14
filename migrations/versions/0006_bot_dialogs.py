"""Диалог с ботом в базе (ревью 14.11.2026, Т-11): bot_dialogs.

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("bot_dialogs"):
        return  # таблицу уже создал create_all при старте приложения
    op.create_table(
        "bot_dialogs",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("event_ids", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_bot_dialogs_updated_at", "bot_dialogs", ["updated_at"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("bot_dialogs"):
        op.drop_index("ix_bot_dialogs_updated_at", "bot_dialogs")
        op.drop_table("bot_dialogs")
