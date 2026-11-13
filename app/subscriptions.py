"""Подписки на новые события (ТЗ §6.2): город и категория, отписка одной командой, без навязчивых уведомлений.

Хранятся только номер чата и фильтры. Правила, чтобы не надоедать:
  * не больше одного сообщения в сутки на чат, все новые события за это время собираются в одну подборку;
  * сообщения только днём (с 9:00 до 21:00 по Уфе);
  * в подборке не больше пяти событий;
  * подписка удаляется сразу при /unsubscribe, при блокировке бота и автоматически через RETENTION_DAYS после создания.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import now_local
from .formatting import is_demo
from .models import Event, Subscription
from .normalize import CATEGORIES
from .search import SearchFilters, _conditions

MAX_PER_CHAT = 10
MAX_IN_DIGEST = 5
RETENTION_DAYS = 365
QUIET_BEFORE, QUIET_AFTER = 9, 21  # часы, в которые рассылка разрешена: [9, 21)


def describe(sub: Subscription) -> str:
    return f"{sub.city or 'Вся республика'} · {CATEGORIES.get(sub.category, 'любая категория') if sub.category else 'любая категория'}"


def add(session: Session, chat_id: int, city: str = "", category: str = "") -> str:
    """'added' | 'exists' | 'limit'. Категория проверяется по справочнику: чужие значения не принимаются."""
    if category and category not in CATEGORIES:
        raise ValueError(f"неизвестная категория: {category}")
    taken = list(session.scalars(select(Subscription).where(Subscription.chat_id == chat_id)))
    if any(s.city == city and s.category == category for s in taken):
        return "exists"
    if len(taken) >= MAX_PER_CHAT:
        return "limit"
    session.add(Subscription(chat_id=chat_id, city=city, category=category))
    session.commit()
    return "added"


def list_for(session: Session, chat_id: int) -> list[Subscription]:
    return list(session.scalars(select(Subscription).where(Subscription.chat_id == chat_id).order_by(Subscription.id)))


def remove(session: Session, chat_id: int, sub_id: int) -> bool:
    """Удаляет одну подписку, но только если она принадлежит этому чату."""
    removed = session.execute(delete(Subscription).where(Subscription.id == sub_id, Subscription.chat_id == chat_id)).rowcount
    session.commit()
    return bool(removed)


def remove_all(session: Session, chat_id: int) -> int:
    """Отписка одной командой: удаляет все подписки чата, возвращает их число."""
    removed = session.execute(delete(Subscription).where(Subscription.chat_id == chat_id)).rowcount
    session.commit()
    return removed or 0


def purge_expired(session: Session, now: datetime | None = None) -> int:
    now = now or now_local()
    removed = session.execute(delete(Subscription).where(Subscription.created_at < now - timedelta(days=RETENTION_DAYS))).rowcount
    session.commit()
    return removed or 0


@dataclass
class Digest:
    chat_id: int
    events: list[Event]
    total: int
    sub_ids: list[int] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


def due_digests(session: Session, now: datetime | None = None) -> list[Digest]:
    """Подборки, которые пора отправить. Ничего не отправляет и не помечает: после отправки вызовите mark_sent."""
    now = now or now_local()
    purge_expired(session, now)
    if not QUIET_BEFORE <= now.hour < QUIET_AFTER:
        return []
    by_chat: dict[int, list[Subscription]] = {}
    for sub in session.scalars(select(Subscription).order_by(Subscription.id)):
        by_chat.setdefault(sub.chat_id, []).append(sub)

    digests = []
    for chat_id, subs in by_chat.items():
        if any(s.last_sent_at is not None and s.last_sent_at.date() == now.date() for s in subs):
            continue  # сегодня уже писали: новые события дождутся завтрашней подборки
        found: dict[int, Event] = {}
        labels = []
        for sub in subs:
            conds = _conditions(SearchFilters(city=sub.city or None, category=sub.category or None), now)
            conds.append(Event.created_at > sub.checked_at)
            events = [e for e in session.scalars(select(Event).where(*conds).order_by(Event.start_at, Event.id)) if not is_demo(e)]
            if events:
                labels.append(describe(sub))
                found.update({e.id: e for e in events})
        ordered = sorted(found.values(), key=lambda e: (e.start_at, e.id))
        if not ordered:
            for sub in subs:  # новых нет: просто двигаем отметку, чтобы не пересматривать старое
                sub.checked_at = now
            session.commit()
            continue
        digests.append(Digest(chat_id, ordered[:MAX_IN_DIGEST], len(ordered), [s.id for s in subs], labels))
    return digests


def mark_sent(session: Session, digest: Digest, now: datetime | None = None) -> None:
    now = now or now_local()
    for sub in session.scalars(select(Subscription).where(Subscription.id.in_(digest.sub_ids))):
        sub.checked_at = sub.last_sent_at = now
    session.commit()
