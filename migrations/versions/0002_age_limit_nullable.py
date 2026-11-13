"""Возраст «не указан» хранится как NULL, а 0 остаётся явным «0+».

Раньше столбец был NOT NULL DEFAULT 0, и «не указан» нельзя было отличить от «0+»: фильтр «с ребёнком» считал событие
без ограничения подходящим любому возрасту. Существующие нули заменяет `python -m app.maintenance ages --apply`.

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.alter_column("age_limit", existing_type=sa.Integer(), nullable=True, server_default=None)
    # Нули, появившиеся из-за того, что источник возраст не указал, становятся «не указан». Явное «0+» в тексте остаётся 0.
    import re

    explicit = re.compile(r"(?<!\d)0\s*\+")
    connection = op.get_bind()
    unknown = [row.id for row in connection.execute(sa.text("SELECT id, title, description FROM events WHERE age_limit = 0"))
               if not explicit.search(f"{row.title} {row.description or ''}")]
    for start in range(0, len(unknown), 500):
        chunk = unknown[start:start + 500]
        connection.execute(sa.text("UPDATE events SET age_limit = NULL WHERE id IN (%s)" % ",".join(str(int(i)) for i in chunk)))


def downgrade() -> None:
    op.execute("UPDATE events SET age_limit = 0 WHERE age_limit IS NULL")
    with op.batch_alter_table("events") as batch:
        batch.alter_column("age_limit", existing_type=sa.Integer(), nullable=False, server_default="0")
