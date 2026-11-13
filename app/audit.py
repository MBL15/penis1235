"""Журнал изменений (ТЗ §6.1): кто, когда и что изменил. Записи пишутся в той же транзакции, что и сама правка."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog, Event
from .observability import request_id_var

# Поля, изменения которых попадают в журнал. Служебные (search_text, dedup_key, updated_at) не нужны: они выводятся из этих.
TRACKED = (
    "title", "description", "category", "city", "venue", "address", "start_at", "end_at", "series_until", "price_min",
    "price_max", "age_limit", "kids_friendly", "url", "image_url", "status", "publication",
)


def _plain(value):
    return value.isoformat() if isinstance(value, datetime) else value


def snapshot(event: Event) -> dict:
    data = {field: _plain(getattr(event, field)) for field in TRACKED}
    data["sources"] = [{"name": s.get("name"), "status": s.get("status")} for s in event.sources or []]
    return data


def diff(before: dict, after: dict) -> dict:
    """{поле: [было, стало]} только по изменившимся полям. Длинные тексты в журнале обрезаются."""
    def short(value):
        return value[:300] + "…" if isinstance(value, str) and len(value) > 300 else value

    return {k: [short(before.get(k)), short(after.get(k))] for k in after if before.get(k) != after.get(k)}


def record(session: Session, actor: str, action: str, event_id: int | None, changes: dict | None = None) -> AuditLog:
    """Добавляет запись в сессию; коммит делает вызывающий код вместе с самой правкой."""
    entry = AuditLog(actor=(actor or "system")[:100], action=action, event_id=event_id, changes=changes or {},
                     request_id=request_id_var.get() if request_id_var.get() != "-" else "")
    session.add(entry)
    return entry


def entries(session: Session, *, event_id: int | None = None, actor: str | None = None, limit: int = 100, offset: int = 0):
    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if event_id is not None:
        stmt = stmt.where(AuditLog.event_id == event_id)
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    return list(session.scalars(stmt.limit(max(1, min(limit, 500))).offset(max(0, offset))))
