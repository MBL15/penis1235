"""Форма обратной связи и заявки организаторов (ТЗ §6.3) с защитой от спама и минимумом персональных данных.

Защита от спама (несколько слоёв, ни один не требует от человека ничего лишнего):
  1. скрытое поле-ловушка website: люди его не видят, роботы заполняют; такая заявка «принимается», но не сохраняется;
  2. метка времени формы: форма подписана сервером, отправка быстрее MIN_SECONDS или позже MAX_SECONDS отклоняется;
  3. лимит с одного адреса (5 в час) и общий лимит на весь сервис;
  4. не больше двух ссылок в тексте, текст должен содержать буквы;
  5. повтор того же текста в течение суток не сохраняется второй раз.
Персональные данные: контакт необязателен, адрес и устройство не сохраняются, в логи текст обращений не попадает.
Срок хранения: обработанные обращения 90 дней, остальные 180 дней."""
import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import audit
from .config import now_local
from .models import Feedback, User
from .security import RateLimiter

KINDS = {
    "organizer": "Заявка организатора: добавить или уточнить событие",
    "correction": "Ошибка в афише",
    "question": "Вопрос о сервисе",
    "other": "Другое",
}
MIN_SECONDS, MAX_SECONDS = 3, 2 * 3600
RETENTION_DAYS, RETENTION_PROCESSED_DAYS = 180, 90
MAX_LINKS = 2

_SECRET = secrets.token_bytes(32)  # у каждого запуска сервера свой: метки после перезапуска недействительны, форма просто обновится
feedback_limiter = RateLimiter(limit=5, window=3600, detail="Вы уже отправили несколько обращений. Попробуйте позже")
feedback_global_limiter = RateLimiter(limit=120, window=3600, detail="Приём обращений временно перегружен. Попробуйте позже")
_LINK = re.compile(r"https?://|www\.", re.I)


class FeedbackIn(BaseModel):
    kind: Literal["organizer", "correction", "question", "other"] = "other"
    event_id: int | None = Field(None, ge=1, le=2**31 - 1, description="Номер события, если обращение о нём")
    message: str = Field(min_length=10, max_length=2000, description="Текст обращения, от 10 до 2000 символов")
    contact: str = Field("", max_length=200, description="Как вам ответить: почта или Telegram. Необязательно")
    token: str = Field("", max_length=200, description="Метка формы из GET /feedback/form")
    website: str = Field("", max_length=200, description="Поле-ловушка для роботов: оставьте пустым")

    @field_validator("message", "contact")
    @classmethod
    def _clean(cls, value: str) -> str:
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value).strip()


class FeedbackAccepted(BaseModel):
    ok: bool = True
    message: str


class FeedbackOut(BaseModel):
    id: int
    created_at: datetime
    kind: str
    kind_label: str
    event_id: int | None = None
    message: str
    contact: str = ""
    status: str
    processed_by: str = ""
    processed_at: datetime | None = None
    user_id: int | None = None
    author_role: str = ""  # роль автора-аккаунта (organizer, user…), пусто у анонимных обращений
    reply: str = ""
    replied_at: datetime | None = None


STATUS_LABELS = {"new": "Принято", "in_progress": "В работе", "processed": "Обработано"}
MAX_REPLY = 1000


def _sign(issued: int) -> str:
    return f"{issued}.{hmac.new(_SECRET, str(issued).encode(), hashlib.sha256).hexdigest()[:32]}"


def issue_token(now: float | None = None) -> str:
    return _sign(int(now if now is not None else time.time()))


def check_token(token: str, now: float | None = None) -> str | None:
    """None, если метка в порядке; иначе причина отказа по-русски."""
    issued_text, _, signature = (token or "").partition(".")
    valid_number = issued_text.isascii() and issued_text.isdigit() and len(issued_text) <= 12  # «²».isdigit() тоже истинно
    if not valid_number or not hmac.compare_digest(_sign(int(issued_text)).partition(".")[2], signature):
        return "Страница формы устарела. Обновите её и отправьте обращение ещё раз"
    age = (now if now is not None else time.time()) - int(issued_text)
    if age < MIN_SECONDS:
        return "Слишком быстро. Проверьте текст и отправьте обращение ещё раз"
    if age > MAX_SECONDS:
        return "Форма открыта слишком давно. Обновите страницу и отправьте обращение ещё раз"
    return None


def check_content(message: str) -> str | None:
    if len(_LINK.findall(message)) > MAX_LINKS:
        return f"В обращении не больше {MAX_LINKS} ссылок"
    if not re.search(r"[A-Za-zА-Яа-яЁё]{3}", message):
        return "Опишите обращение словами"
    return None


def purge_old(session: Session, now: datetime | None = None) -> int:
    """Удаляет обращения по сроку хранения."""
    now = now or now_local()
    removed = session.execute(delete(Feedback).where(
        (Feedback.created_at < now - timedelta(days=RETENTION_DAYS))
        | ((Feedback.status == "processed") & (Feedback.processed_at < now - timedelta(days=RETENTION_PROCESSED_DAYS)))
    )).rowcount or 0
    session.commit()
    return removed


def save_feedback(session: Session, data: FeedbackIn, now: datetime | None = None, *, user_id: int | None = None) -> tuple[Feedback | None, str]:
    """(обращение, результат): saved | duplicate. Повтор того же текста за сутки второй раз не сохраняется.
    Если человек вошёл в аккаунт, обращение привязывается к нему: он увидит статус и ответ в кабинете."""
    now = now or now_local()
    digest = hashlib.sha1(f"{data.kind}|{data.event_id}|{data.message.lower()}".encode("utf-8")).hexdigest()
    same = session.scalar(select(Feedback).where(Feedback.message_hash == digest, Feedback.created_at > now - timedelta(days=1)))
    if same is not None:
        return same, "duplicate"
    item = Feedback(kind=data.kind, event_id=data.event_id, message=data.message, contact=data.contact, message_hash=digest,
                    created_at=now, user_id=user_id)
    session.add(item)
    session.commit()
    return item, "saved"


def _out(item: Feedback, roles: dict[int, str] | None = None) -> FeedbackOut:
    return FeedbackOut(id=item.id, created_at=item.created_at, kind=item.kind, kind_label=KINDS.get(item.kind, item.kind),
                       event_id=item.event_id, message=item.message, contact=item.contact or "", status=item.status,
                       processed_by=item.processed_by or "", processed_at=item.processed_at, user_id=item.user_id,
                       author_role=(roles or {}).get(item.user_id, "") if item.user_id else "", reply=item.reply or "",
                       replied_at=item.replied_at)


def _roles(session: Session, items: list[Feedback]) -> dict[int, str]:
    ids = {i.user_id for i in items if i.user_id}
    return {u.id: u.role for u in session.scalars(select(User).where(User.id.in_(ids)))} if ids else {}


def list_feedback(session: Session, *, status: str = "new", limit: int = 50) -> list[FeedbackOut]:
    purge_old(session)
    stmt = select(Feedback).order_by(Feedback.id.desc())
    if status != "all":
        stmt = stmt.where(Feedback.status == status)
    items = list(session.scalars(stmt.limit(max(1, min(limit, 200)))))
    roles = _roles(session, items)
    return [_out(item, roles) for item in items]


def list_own(session: Session, user_id: int, *, limit: int = 100) -> list[FeedbackOut]:
    """Обращения одного аккаунта со статусом и ответом модератора."""
    items = list(session.scalars(select(Feedback).where(Feedback.user_id == user_id).order_by(Feedback.id.desc()).limit(limit)))
    return [_out(item) for item in items]


def set_feedback_status(session: Session, feedback_id: int, status: str | None, actor: str, reply: str | None = None) -> FeedbackOut | None:
    """Меняет статус и/или записывает ответ автору. Ответ уйдёт автору-аккаунту в Telegram (бот, app.bot.send_feedback_replies)."""
    item = session.get(Feedback, feedback_id)
    if item is None:
        return None
    changes: dict = {}
    if status is not None and item.status != status:
        changes["status"] = [item.status, status]
        item.status = status
        item.processed_by, item.processed_at = (actor, now_local()) if status == "processed" else ("", None)
    if reply is not None and reply.strip() != (item.reply or ""):
        item.reply, item.replied_at, item.notified_at = reply.strip()[:MAX_REPLY] or None, now_local(), None
        changes["reply"] = "ответ записан"  # текст ответа в журнал не копируем: он адресован автору
        if not item.reply:
            item.replied_at = None
    if changes:
        audit.record(session, actor, "feedback", None, {"feedback_id": item.id, **changes})
        session.commit()
    return _out(item, _roles(session, [item]))


def pending_replies(session: Session, limit: int = 50) -> list[Feedback]:
    """Обращения аккаунтов, на которые ответили, а автору ещё не сообщили. Рассылку делает бот."""
    return list(session.scalars(select(Feedback).where(
        Feedback.user_id.is_not(None), Feedback.reply.is_not(None), Feedback.notified_at.is_(None)).order_by(Feedback.id).limit(limit)))
