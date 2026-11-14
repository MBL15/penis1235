"""Вход на сайт через бота: login_requests.

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("login_requests"):
        return  # таблицу уже создал create_all при старте приложения
    op.create_table(
        "login_requests",
        sa.Column("code_hash", sa.String(64), primary_key=True),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger()),
        sa.Column("display_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("confirmed_at", sa.DateTime()),
        sa.Column("used_at", sa.DateTime()),
    )
    op.create_index("ix_login_requests_created_at", "login_requests", ["created_at"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("login_requests"):
        op.drop_index("ix_login_requests_created_at", "login_requests")
        op.drop_table("login_requests")
