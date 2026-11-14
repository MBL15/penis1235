"""Аккаунты и роли (docs/08). Сайт работает без аккаунта; аккаунт даёт историю посещений, подписки и сохранённые поиски
на любом устройстве и роли из ТЗ §3.

Вход только через Telegram: подпись мини-приложения (initData) или кнопка «Войти через Telegram» проверяются токеном бота,
паролей нет. Вход живёт в куке HttpOnly; в базе лежит только хеш токена.

Персональные данные (ТЗ §10, красная линия §13): номер Telegram, имя оттуда же, роль, согласие на историю. История посещений
пишется только при согласии, хранится HISTORY_RETENTION_DAYS дней, стирается кнопкой; аккаунт удаляется целиком."""
import hashlib
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from . import audit, config
from .config import now_local
from .deps import SessionDep
from .models import ApiKey, Event, Feedback, LoginRequest, SavedSearch, Subscription, User, UserSession, VisitHistory
from .security import RateLimiter, auth_limiter, partner_limiter
from .telegram_auth import TelegramUser

SESSION_COOKIE = "afisha_session"
ROLES = {"user": "Житель или гость", "organizer": "Организатор", "moderator": "Модератор", "ministry": "Министерство"}
MAX_SESSIONS = 10  # входов на одного человека одновременно: старые закрываются сами
HISTORY_DEDUP = timedelta(hours=24)  # повторное открытие той же карточки за сутки в историю не пишется
SEEN_EVERY = timedelta(minutes=10)  # last_seen_at обновляется не чаще: иначе каждый запрос писал бы в базу
MAX_SAVED_SEARCHES = 20
MAX_KEYS_PER_USER = 5
DEFAULT_KEY_LIMIT = 600  # запросов в минуту
MAX_KEY_LIMIT = 6000
KEY_PREFIX = "afk_"
LOGIN_TTL = timedelta(minutes=5)  # вход через бота: столько живёт ссылка t.me/<бот>?start=login_<код>
LOGIN_CODE = re.compile(r"^[A-Za-z0-9_-]{16,40}$")


# ---------- вход и сессии ----------

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sign_in(session: Session, tg: TelegramUser, *, history: bool | None = None) -> tuple[User, bool]:
    """Находит или создаёт аккаунт по номеру Telegram. Возвращает (аккаунт, создан ли сейчас).

    Новый аккаунт без согласия на историю: оно включается только явным `history=True`. Роль, выданная заранее администратором
    (аккаунт без имени), сохраняется. Подписки бота с этим номером чата привязываются к аккаунту."""
    now = now_local()
    user = session.scalar(select(User).where(User.telegram_id == tg.id))
    created = user is None
    if user is None:
        user = User(telegram_id=tg.id, display_name=tg.name, role="user", created_at=now)
        session.add(user)
    else:
        created = not user.display_name  # роль выдали заранее, человек входит впервые
        user.display_name = tg.name
    user.last_seen_at = now
    if history is not None and (created or not user.consent_at):
        user.history_enabled, user.consent_at = history, (now if history else None)
    session.flush()
    session.execute(update(Subscription).where(Subscription.chat_id == tg.id, Subscription.user_id.is_(None)).values(user_id=user.id))
    session.commit()
    return user, created


def start_session(session: Session, user: User) -> str:
    """Новый вход: возвращает токен для куки (в базе только хеш). Просроченные и лишние старые входы удаляются."""
    now = now_local()
    session.execute(delete(UserSession).where(UserSession.expires_at < now))
    old = list(session.scalars(select(UserSession.token_hash).where(UserSession.user_id == user.id)
                               .order_by(UserSession.created_at.desc(), UserSession.token_hash).offset(MAX_SESSIONS - 1)))
    if old:
        session.execute(delete(UserSession).where(UserSession.token_hash.in_(old)))
    token = secrets.token_urlsafe(32)
    session.add(UserSession(token_hash=hash_token(token), user_id=user.id, created_at=now,
                            expires_at=now + timedelta(days=config.SESSION_DAYS)))
    session.commit()
    return token


def end_session(session: Session, token: str) -> None:
    session.execute(delete(UserSession).where(UserSession.token_hash == hash_token(token)))
    session.commit()


def set_login_cookie(response: Response, token: str, *, embedded: bool = False) -> None:
    """Кука входа: HttpOnly (скрипт страницы её не прочтёт), SameSite=Lax. Мини-приложение в веб-версии Telegram живёт во
    фрейме чужого сайта, там Lax не работает: для него (и только при https) SameSite=None. Чужие изменяющие запросы
    всё равно отсекает app.security.cross_site_write."""
    secure = config.PUBLIC_URL.startswith("https://")
    response.set_cookie(SESSION_COOKIE, token, max_age=config.SESSION_DAYS * 86400, httponly=True, secure=secure,
                        samesite="none" if (embedded and secure) else "lax", path="/")


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def user_for_token(session: Session, token: str | None) -> User | None:
    if not token or len(token) > 200:
        return None
    now = now_local()
    row = session.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    if row is None or row.expires_at < now:
        return None
    user = session.get(User, row.user_id)
    if user is not None and (user.last_seen_at is None or now - user.last_seen_at > SEEN_EVERY):
        user.last_seen_at = now
        session.commit()
    return user


def optional_user(request: Request, session: SessionDep) -> User | None:
    """Кто вошёл по куке; None для посетителя без аккаунта. FastAPI вычисляет зависимость один раз на запрос."""
    return user_for_token(session, request.cookies.get(SESSION_COOKIE))


OptionalUser = Annotated[User | None, Depends(optional_user)]


def require_user(user: OptionalUser) -> User:
    if user is None:
        raise HTTPException(401, "Нужен вход: откройте «Кабинет» и войдите через Telegram")
    return user


CurrentUser = Annotated[User, Depends(require_user)]


def require_role(*roles: str):
    """Зависимость: вошёл человек с одной из ролей. Роль читается из базы при каждом запросе, отзыв действует сразу."""

    def check(user: CurrentUser) -> User:
        if user.role not in roles:
            raise HTTPException(403, "Для этого действия нужна другая роль: " + ", ".join(ROLES[r] for r in roles))
        return user

    return check


def role_label(role: str) -> str:
    return ROLES.get(role, role)


# ---------- роли ----------

def set_role(session: Session, telegram_id: int, role: str, actor: str, *, display_name: str = "") -> User:
    """Назначает роль по номеру Telegram, даже если человек ещё не входил (аккаунт создаётся без имени, роль ждёт его)."""
    if role not in ROLES:
        raise ValueError(f"Неизвестная роль: {role}. Допустимые: {', '.join(ROLES)}")
    if telegram_id <= 0:
        raise ValueError("Номер Telegram должен быть положительным числом")
    user = session.scalar(select(User).where(User.telegram_id == telegram_id))
    old = user.role if user else None
    if user is None:
        user = User(telegram_id=telegram_id, display_name=display_name[:100], role=role, created_at=now_local())
        session.add(user)
    else:
        user.role = role
    session.flush()
    if old != role:
        audit.record(session, actor, "role", None, {"user_id": user.id, "telegram_id": telegram_id, "role": [old, role]})
    session.commit()
    return user


def revoke_access(session: Session, user: User, actor: str) -> None:
    """Отзыв доступа: роль снова «житель», все входы закрыты, личные ключи отозваны."""
    old = user.role
    user.role = "user"
    session.execute(delete(UserSession).where(UserSession.user_id == user.id))
    session.execute(update(ApiKey).where(ApiKey.owner_user_id == user.id, ApiKey.revoked_at.is_(None)).values(revoked_at=now_local()))
    audit.record(session, actor, "role", None, {"user_id": user.id, "telegram_id": user.telegram_id, "role": [old, "user"], "revoked": True})
    session.commit()


# ---------- история посещений ----------

def record_visit(session: Session, user: User, event_id: int, kind: str = "view") -> bool:
    """Пишет посещение, если человек согласился вести историю. Не пишет повтор за сутки и неопубликованные карточки."""
    if not user.history_enabled or kind not in ("view", "ticket"):
        return False
    event = session.get(Event, event_id)
    if event is None or event.publication not in (None, "published"):
        return False
    now = now_local()
    seen = session.scalar(select(VisitHistory.id).where(
        VisitHistory.user_id == user.id, VisitHistory.event_id == event_id, VisitHistory.kind == kind,
        VisitHistory.viewed_at > now - HISTORY_DEDUP).limit(1))
    if seen is not None:
        return False
    session.add(VisitHistory(user_id=user.id, event_id=event_id, kind=kind, viewed_at=now))
    session.commit()
    return True


def list_history(session: Session, user: User, *, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    total = session.scalar(select(func.count()).select_from(VisitHistory).where(VisitHistory.user_id == user.id)) or 0
    rows = session.execute(
        select(VisitHistory, Event).outerjoin(Event, Event.id == VisitHistory.event_id).where(VisitHistory.user_id == user.id)
        .order_by(VisitHistory.viewed_at.desc(), VisitHistory.id.desc()).limit(max(1, min(limit, 200))).offset(max(0, offset))).all()
    items = [{"id": v.id, "event_id": v.event_id, "kind": v.kind, "viewed_at": v.viewed_at, "available": e is not None,
              "title": e.title if e else "", "city": e.city if e else "", "venue": e.venue if e else "",
              "start_at": e.start_at if e else None, "status": e.status if e else ""} for v, e in rows]
    return items, total


def clear_history(session: Session, user: User) -> int:
    removed = session.execute(delete(VisitHistory).where(VisitHistory.user_id == user.id)).rowcount or 0
    session.commit()
    return removed


def purge_history(session: Session, now: datetime | None = None) -> int:
    """Срок хранения: старше HISTORY_RETENTION_DAYS дней записи удаляются (при старте и при чтении истории)."""
    now = now or now_local()
    removed = session.execute(delete(VisitHistory).where(
        VisitHistory.viewed_at < now - timedelta(days=config.HISTORY_RETENTION_DAYS))).rowcount or 0
    session.commit()
    return removed


def purge_sessions(session: Session, now: datetime | None = None) -> int:
    removed = session.execute(delete(UserSession).where(UserSession.expires_at < (now or now_local()))).rowcount or 0
    session.commit()
    return removed


def set_history_consent(session: Session, user: User, enabled: bool) -> User:
    """Согласие на историю. Выключение стирает уже собранное: хранить без согласия нельзя."""
    user.history_enabled = enabled
    user.consent_at = now_local() if enabled else None
    if not enabled:
        session.execute(delete(VisitHistory).where(VisitHistory.user_id == user.id))
    session.commit()
    return user


# ---------- сохранённые поиски ----------

SEARCH_FIELDS: dict[str, Any] = {
    "q": ("str", 200), "city": ("str", 64), "venue": ("str", 200), "category": ("str", 200), "when": ("str", 16),
    "date_from": ("date", 0), "date_to": ("date", 0), "price_max": ("int", 1_000_000), "age": ("int", 99),
    "free": ("bool", 0), "kids": ("bool", 0),
}


def clean_filters(raw: dict) -> dict:
    """Оставляет только известные условия поиска и проверяет значения: в базу не попадает произвольный JSON."""
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in SEARCH_FIELDS:
            raise ValueError(f"Неизвестное условие поиска: {str(key)[:40]}")
        kind, limit = SEARCH_FIELDS[key]
        if value in (None, "", False):
            continue
        if kind == "str" and isinstance(value, str):
            clean[key] = value.strip()[:limit]
        elif kind == "int" and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= limit:
            clean[key] = value
        elif kind == "bool" and value is True:
            clean[key] = True
        elif kind == "date" and isinstance(value, str):
            try:
                clean[key] = date.fromisoformat(value).isoformat()
            except ValueError as exc:
                raise ValueError(f"Дата в условии «{key}» должна быть ГГГГ-ММ-ДД") from exc
        else:
            raise ValueError(f"Неверное значение условия «{key}»")
    if not clean:
        raise ValueError("Укажите хотя бы одно условие поиска")
    return clean


# ---------- экспорт и удаление ----------

def export_user_data(session: Session, user: User) -> dict:
    """Всё, что сервис хранит о человеке (право на копию данных). Хеши ключей и входов не отдаются."""
    all_history, offset = [], 0
    while True:  # постранично: право на копию данных — это вся история, а не первые строки
        page, total = list_history(session, user, limit=200, offset=offset)
        all_history += [{"event_id": h["event_id"], "title": h["title"], "kind": h["kind"], "viewed_at": h["viewed_at"]} for h in page]
        offset += 200
        if offset >= total:
            break
    return {
        "exported_at": now_local(),
        "profile": {"id": user.id, "telegram_id": user.telegram_id, "display_name": user.display_name, "role": user.role,
                    "created_at": user.created_at, "history_enabled": user.history_enabled, "consent_at": user.consent_at},
        "history": all_history,
        "saved_searches": [{"id": s.id, "name": s.name, "filters": s.filters, "created_at": s.created_at}
                           for s in session.scalars(select(SavedSearch).where(SavedSearch.user_id == user.id).order_by(SavedSearch.id))],
        "subscriptions": [{"id": s.id, "city": s.city, "category": s.category, "created_at": s.created_at}
                          for s in session.scalars(select(Subscription).where(
                              (Subscription.user_id == user.id) | (Subscription.chat_id == user.telegram_id)).order_by(Subscription.id))],
        "feedback": [{"id": f.id, "kind": f.kind, "event_id": f.event_id, "message": f.message, "status": f.status, "reply": f.reply or "",
                      "created_at": f.created_at} for f in session.scalars(select(Feedback).where(Feedback.user_id == user.id).order_by(Feedback.id))],
        "api_keys": [{"id": k.id, "name": k.name, "prefix": k.key_prefix, "rate_limit": k.rate_limit, "created_at": k.created_at,
                      "revoked_at": k.revoked_at, "requests_total": k.requests_total}
                     for k in session.scalars(select(ApiKey).where(ApiKey.owner_user_id == user.id).order_by(ApiKey.id))],
    }


def delete_account(session: Session, user: User) -> None:
    """Стирает аккаунт, историю, сохранённые поиски, подписки, входы и ключи. Обращения остаются без автора и без контакта:
    их текст живёт до конца обычного срока хранения (app.feedback.RETENTION_DAYS)."""
    uid = user.id
    for model in (UserSession, VisitHistory, SavedSearch):
        session.execute(delete(model).where(model.user_id == uid))
    session.execute(delete(Subscription).where((Subscription.user_id == uid) | (Subscription.chat_id == user.telegram_id)))
    session.execute(delete(ApiKey).where(ApiKey.owner_user_id == uid))
    session.execute(update(Feedback).where(Feedback.user_id == uid).values(user_id=None, contact=""))
    session.delete(user)
    session.commit()


# ---------- ключи Министерства ----------

def create_api_key(session: Session, *, name: str, owner_user_id: int | None, rate_limit: int = DEFAULT_KEY_LIMIT) -> tuple[ApiKey, str]:
    """Новый ключ только для чтения. Возвращает (запись, сам ключ): ключ показывается один раз, в базе остаётся хеш."""
    token = KEY_PREFIX + secrets.token_urlsafe(24)
    key = ApiKey(key_hash=hash_token(token), key_prefix=token[:8], name=name.strip()[:80], owner_user_id=owner_user_id,
                 rate_limit=max(1, min(rate_limit, MAX_KEY_LIMIT)), created_at=now_local())
    session.add(key)
    session.commit()
    return key, token


def revoke_api_key(session: Session, key: ApiKey) -> None:
    if key.revoked_at is None:
        key.revoked_at = now_local()
        session.commit()


_pending: dict[int, int] = {}
_flushed: dict[int, float] = {}
_pending_lock = threading.Lock()
FLUSH_EVERY = 30.0  # секунд: счётчик запросов пишется в базу пачкой, а не на каждый запрос


def count_use(session: Session, key: ApiKey) -> None:
    with _pending_lock:
        _pending[key.id] = _pending.get(key.id, 0) + 1
        if time.monotonic() - _flushed.get(key.id, 0.0) < FLUSH_EVERY:
            return
        used, _pending[key.id] = _pending[key.id], 0
        _flushed[key.id] = time.monotonic()
    session.execute(update(ApiKey).where(ApiKey.id == key.id).values(requests_total=ApiKey.requests_total + used, last_used_at=now_local()))
    session.commit()


def partner_key(request: Request, session: SessionDep) -> None:
    """Заголовок X-Partner-Key необязателен. Если он есть, ключ проверяется и запросы считаются по лимиту самого ключа;
    неверный или отозванный ключ даёт 401, а подбор с одного адреса останавливается тем же лимитом, что и у ключей модераторов."""
    token = request.headers.get("x-partner-key")
    if token is None:
        return
    ip = RateLimiter.client_ip(request)
    if auth_limiter.blocked(ip):
        raise HTTPException(429, auth_limiter.detail, headers={"Retry-After": "60"})
    key = session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_token(token.strip()), ApiKey.revoked_at.is_(None))) if token else None
    if key is None:
        auth_limiter.allow(ip)
        raise HTTPException(401, "Неверный или отозванный ключ в заголовке X-Partner-Key")
    if not partner_limiter.allow(f"key:{key.id}", limit=key.rate_limit):
        raise HTTPException(429, f"Превышен лимит ключа: {key.rate_limit} запросов в минуту", headers={"Retry-After": "60"})
    count_use(session, key)


PartnerKeyDep = Depends(partner_key)


def key_view(key: ApiKey) -> dict:
    with _pending_lock:
        pending = _pending.get(key.id, 0)
    return {"id": key.id, "name": key.name, "prefix": key.key_prefix, "rate_limit": key.rate_limit, "created_at": key.created_at,
            "revoked_at": key.revoked_at, "last_used_at": key.last_used_at, "requests_total": key.requests_total + pending,
            "owner_user_id": key.owner_user_id}


def clean_name(value: str, fallback: str = "") -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "", value or "").strip()[:80] or fallback


# ---------- вход через бота ----------

def start_bot_login(session: Session) -> tuple[str, str]:
    """Новый запрос входа: (код для ссылки в бот, секрет браузера для куки). В базе только их хеши."""
    now = now_local()
    session.execute(delete(LoginRequest).where(LoginRequest.expires_at < now - timedelta(hours=1)))
    code, secret = secrets.token_urlsafe(16), secrets.token_urlsafe(32)
    session.add(LoginRequest(code_hash=hash_token(code), browser_hash=hash_token(secret), created_at=now, expires_at=now + LOGIN_TTL))
    session.commit()
    return code, secret


def pending_bot_login(session: Session, code: str) -> LoginRequest | None:
    """Запрос, который ещё можно подтвердить в боте: не просрочен, не подтверждён и не использован."""
    if not LOGIN_CODE.fullmatch(code or ""):
        return None
    row = session.get(LoginRequest, hash_token(code))
    if row is None or row.expires_at < now_local() or row.confirmed_at or row.used_at:
        return None
    return row


def confirm_bot_login(session: Session, code: str, tg: TelegramUser) -> bool:
    """Человек нажал «Подтвердить» в боте: запрос запоминает, кто он. Номер берётся из самого Telegram, не из ссылки."""
    row = pending_bot_login(session, code)
    if row is None:
        return False
    row.telegram_id, row.display_name, row.confirmed_at = tg.id, tg.name[:100], now_local()
    session.commit()
    return True


def cancel_bot_login(session: Session, code: str) -> None:
    row = pending_bot_login(session, code)
    if row is not None:
        row.expires_at = now_local()
        session.commit()


def finish_bot_login(session: Session, code: str, secret: str | None) -> tuple[str, User | None]:
    """Страница спрашивает, подтверждён ли вход: ('pending' | 'expired' | 'denied' | 'ok', аккаунт). Забрать вход можно один раз
    и только из браузера, который его начал (секрет в куке): чужая ссылка не войдёт за другого."""
    if not LOGIN_CODE.fullmatch(code or "") or not secret or len(secret) > 200:
        return "denied", None
    row = session.get(LoginRequest, hash_token(code))
    if row is None or not secrets.compare_digest(row.browser_hash, hash_token(secret)):
        return "denied", None
    if row.used_at:
        return "expired", None
    if row.confirmed_at is None:
        return ("expired", None) if row.expires_at < now_local() else ("pending", None)
    row.used_at = now_local()
    session.commit()
    user, _ = sign_in(session, TelegramUser(int(row.telegram_id), row.display_name or f"Пользователь {row.telegram_id}"))
    return "ok", user

