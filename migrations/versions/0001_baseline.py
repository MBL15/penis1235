"""Исходная схема: состояние базы на конец этапа P2 (до появления миграций).

Revision ID: 0001
Revises:
"""
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ничего не делает: таблицы этой версии создают create_all (новая база) и add_missing_columns (старая, до миграций).
    pass


def downgrade() -> None:
    pass
