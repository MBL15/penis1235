"""События, пропавшие из источника (ТЗ §6.5, §14): помечаются, а не удаляются молча.

Источник мог убрать событие (отменили, перенесли, ошибка). Каждый раз, когда источник присылает событие, в его записи у события
обновляется `seen_at`. После полного прохода источника события, которых он больше не прислал, получают у этого источника
`vanished_at`. Если пропали у всех своих источников, событие уходит в очередь модерации с причиной «пропало: …», потому что
решать, снимать ли его, должен человек. Вернулось в источник: помета снимается и событие публикуется обратно.

Защита от ложных срабатываний: помечаем только если проход получил не меньше половины записей прошлого прохода (пустой ответ,
блокировка сайта или обрыв не должны снять с публикации всю афишу) и только будущие события."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .config import now_local
from .models import Event

PREFIX = "пропало: "
MIN_SHARE = 0.5


def _stamp(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def mark_vanished(session: Session, source: str, started_at: datetime, *, actor: str = "sync") -> int:
    """Помечает события источника, которых не было в проходе, начавшемся в started_at. Возвращает число помеченных записей."""
    now = now_local()
    count = 0
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for event in session.scalars(select(Event).where(Event.start_at >= today)):
        entries = event.sources or []
        mine = next((s for s in entries if s.get("name") == source), None)
        if mine is None or mine.get("vanished_at") or not mine.get("seen_at") or mine["seen_at"] >= _stamp(started_at):
            continue  # событие этого источника видели в проходе или ещё ни разу не видели с отметкой времени
        before = audit.snapshot(event)
        event.sources = [{**s, "vanished_at": _stamp(now)} if s is mine else s for s in entries]
        count += 1
        if all(s.get("vanished_at") for s in event.sources) and event.publication == "published":
            event.publication = "draft"
            event.review_reason = f"{PREFIX}источник «{source}» больше не присылает это событие"
            audit.record(session, actor, "publication", event.id, audit.diff(before, audit.snapshot(event)))
    session.commit()
    return count


def revive(session: Session, event: Event, source: str, *, actor: str = "sync") -> bool:
    """Источник прислал событие снова: снимает помету. Возвращает True, если событие вернулось в выдачу."""
    entries = event.sources or []
    if not any(s.get("name") == source and s.get("vanished_at") for s in entries):
        return False
    before = audit.snapshot(event)
    event.sources = [{k: v for k, v in s.items() if k != "vanished_at"} if s.get("name") == source else s for s in entries]
    if event.publication == "draft" and (event.review_reason or "").startswith(PREFIX):
        event.publication, event.review_reason = "published", None
        audit.record(session, actor, "publication", event.id, audit.diff(before, audit.snapshot(event)))
        return True
    return False
