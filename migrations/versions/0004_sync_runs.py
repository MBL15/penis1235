"""Таблица запусков сбора по расписанию (sync_runs).

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("sync_runs"):
        return  # таблицу уже создал create_all при старте приложения
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("status", sa.String(12), nullable=False, server_default="running"),
        sa.Column("fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("merged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unchanged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("queued", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("vanished", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revived", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(400), nullable=False, server_default=""),
    )
    op.create_index("ix_sync_runs_job", "sync_runs", ["job"])
    op.create_index("ix_sync_runs_started_at", "sync_runs", ["started_at"])


def downgrade() -> None:
    op.drop_table("sync_runs")
