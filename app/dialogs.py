"""Диалог с ботом в базе (ТЗ §6.4: память не менее пяти реплик): история и события прошлого ответа переживают перезапуск.

Бот держит их и в памяти процесса (быстро), а сюда пишет после каждого ответа и читает, когда в памяти пусто. Хранится
только текст реплик и номера событий, без имени и других данных; через RETENTION_HOURS после последней реплики диалог
считается законченным и удаляется."""
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from .config import now_local
from .models import BotDialog

MAX_TURNS = 10  # реплик (вопрос и ответ — две): столько же, сколько держит бот в памяти
MAX_CHARS = 2000
RETENTION_HOURS = 24


def load(session: Session, chat_id: int, now: datetime | None = None) -> tuple[list[dict], list[int]]:
    row = session.get(BotDialog, chat_id)
    if row is None or row.updated_at < (now or now_local()) - timedelta(hours=RETENTION_HOURS):
        return [], []
    history = [t for t in row.history or [] if isinstance(t, dict) and t.get("role") in ("user", "assistant")]
    return history[-MAX_TURNS:], [i for i in row.event_ids or [] if isinstance(i, int)][:10]


def save(session: Session, chat_id: int, history: list[dict], event_ids: list[int] | None, now: datetime | None = None) -> None:
    row = session.get(BotDialog, chat_id) or BotDialog(chat_id=chat_id)
    row.history = [{"role": t["role"], "content": str(t.get("content", ""))[:MAX_CHARS]} for t in history[-MAX_TURNS:]]
    row.event_ids = list(event_ids or [])[:10]
    row.updated_at = now or now_local()
    session.merge(row)
    session.commit()


def forget(session: Session, chat_id: int) -> None:
    session.execute(delete(BotDialog).where(BotDialog.chat_id == chat_id))
    session.commit()


def purge(session: Session, now: datetime | None = None) -> int:
    removed = session.execute(delete(BotDialog).where(BotDialog.updated_at < (now or now_local()) - timedelta(hours=RETENTION_HOURS))).rowcount
    session.commit()
    return removed or 0
