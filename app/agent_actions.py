"""Действия агента, которые меняют данные (ТЗ §6.5): подписка, перенос подписки, обращение модератору.

Агент (модель или правила) только ПРЕДЛАГАЕТ действие: на сервере создаётся запись «ожидает подтверждения», а человек видит
кнопки «Подтвердить» и «Отмена» с текстом, который написал сервер, а не модель. Данные меняются только после нажатия кнопки;
без неё предложение через TTL_MINUTES минут устаревает. Подсказка во вводе, текст события или сам ответ модели подтвердить
действие не могут: у них нет доступа к кнопке."""
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import subscriptions
from .config import now_local
from .feedback import KINDS, FeedbackIn, check_content, feedback_global_limiter, save_feedback
from .models import PendingAction, Subscription
from .search import get_event

import uuid

TTL_MINUTES = 15
ACTION_KINDS = ("subscribe", "move_subscription", "feedback")
CHAT_BOUND = ("subscribe", "move_subscription")  # подписки привязаны к чату Telegram: подтвердить их можно только в нём


def propose(session: Session, kind: str, payload: dict, summary: str, *, trace_id: str = "", now: datetime | None = None) -> PendingAction:
    """Создаёт предложение действия. Ничего не меняет, кроме самой записи о предложении."""
    if kind not in ACTION_KINDS:
        raise ValueError(f"неизвестное действие: {kind}")
    now = now or now_local()
    action = PendingAction(id=uuid.uuid4().hex, created_at=now, expires_at=now + timedelta(minutes=TTL_MINUTES), kind=kind,
                           payload=payload, summary=summary[:1000], trace_id=trace_id)
    session.add(action)
    session.commit()
    return action


def public(action: PendingAction) -> dict:
    return {"id": action.id, "kind": action.kind, "summary": action.summary, "expires_at": action.expires_at.isoformat(timespec="minutes")}


def get_action(session: Session, action_id: str) -> PendingAction | None:
    return session.get(PendingAction, action_id) if len(action_id) == 32 and action_id.isalnum() else None


def _chat_error(action: PendingAction, chat_id: int | None) -> str | None:
    if action.kind in CHAT_BOUND:
        if chat_id is None:
            return "Подписки оформляются в Telegram-боте: откройте бота и подтвердите там."
        if int(action.payload.get("chat_id", -1)) != int(chat_id):
            return "Это действие можно подтвердить только в том чате, где оно было предложено."
    return None


def confirm(session: Session, action_id: str, *, chat_id: int | None = None, now: datetime | None = None) -> tuple[bool, str]:
    """(выполнено, сообщение человеку). Выполняет действие один раз и только если оно ещё не устарело."""
    now = now or now_local()
    action = get_action(session, action_id)
    if action is None:
        return False, "Такого действия нет или оно уже устарело. Повторите просьбу."
    if action.status != "pending":
        return False, "Это действие уже обработано."
    if now > action.expires_at:
        return False, "Время подтверждения истекло. Повторите просьбу."
    problem = _chat_error(action, chat_id)
    if problem:
        return False, problem

    payload = action.payload
    if action.kind == "subscribe":
        result = subscriptions.add(session, int(chat_id), payload.get("city", ""), payload.get("category", ""))
        message = {"added": "Подписка оформлена. Новые события буду присылать не чаще раза в сутки, днём.",
                   "exists": "Такая подписка уже есть.",
                   "limit": f"Можно не больше {subscriptions.MAX_PER_CHAT} подписок: удалите лишнюю командой /subscriptions."}[result]
        ok = result != "limit"
    elif action.kind == "move_subscription":
        subs = subscriptions.list_for(session, int(chat_id))
        if not subs:
            return False, "У вас нет подписок, переносить нечего. Оформить новую: /subscribe."
        city = payload["city"]
        # После переноса подписки одной категории совпадут: оставляем одну, лучше ту, что уже в нужном городе. Лишние удаляются
        # и сбрасываются в базу до переименования остальных: иначе UPDATE упирается в уникальный ключ (чат, город, категория).
        keep: dict[str, Subscription] = {}
        for sub in sorted(subs, key=lambda s: (s.city != city, s.id)):
            if sub.category in keep:
                session.delete(sub)
            else:
                keep[sub.category] = sub
        session.flush()
        for sub in keep.values():
            sub.city = city
        session.commit()
        message, ok = f"Подписки перенесены в: {payload['city'] or 'вся республика'}.", True
    else:  # feedback
        reason = check_content(payload.get("message", ""))
        if reason:
            return False, reason
        if payload.get("event_id") is not None and get_event(session, int(payload["event_id"])) is None:
            return False, "Такого события уже нет: обращение не отправлено."
        if not feedback_global_limiter.allow("all"):
            return False, "Приём обращений временно перегружен. Попробуйте позже."
        save_feedback(session, FeedbackIn(kind=payload.get("kind", "other"), event_id=payload.get("event_id"),
                                          message=payload["message"], contact=""), now)
        message, ok = "Обращение отправлено модератору. Спасибо!", True
    action.status = "confirmed" if ok else "pending"
    session.commit()
    return ok, message


def cancel(session: Session, action_id: str, *, chat_id: int | None = None) -> bool:
    action = get_action(session, action_id)
    if action is None or action.status != "pending" or _chat_error(action, chat_id):
        return False
    action.status = "cancelled"
    session.commit()
    return True


def purge_actions(session: Session, now: datetime | None = None) -> int:
    removed = session.execute(delete(PendingAction).where(PendingAction.expires_at < (now or now_local()) - timedelta(days=1))).rowcount
    session.commit()
    return removed or 0


def summary_subscribe(city: str, category: str) -> str:
    return "Подписка на новые события: " + subscriptions.describe(Subscription(city=city, category=category)) + \
        ". Писать буду не чаще раза в сутки, днём, до пяти событий."


def summary_move(city: str) -> str:
    return f"Перенести все ваши подписки в: {city or 'вся республика'}."


def summary_feedback(kind: str, event_id: int | None, message: str) -> str:
    where = f" о событии №{event_id}" if event_id else ""
    return f"Отправить модератору обращение ({KINDS.get(kind, kind)}){where}: «{message[:300]}»"
