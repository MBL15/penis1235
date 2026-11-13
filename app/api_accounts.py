"""Аккаунты (docs/08): вход через Telegram, кабинет /me, история посещений, сохранённые поиски, обращения организатора,
ключи и статистика Министерства. Методы для администратора (роли, ключи) отдельным роутером: их выдаёт только владелец ключа.
Подключается под /api/v1."""
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from . import accounts, audit, config
from .accounts import CurrentUser, OptionalUser
from .auth import require_key_admin
from .deps import SessionDep
from .feedback import STATUS_LABELS, FeedbackOut, list_own
from .models import ApiKey, Event, Feedback, SavedSearch, Subscription, User
from .schemas import ErrorOut
from .security import RateLimiter, login_limiter
from .telegram_auth import parse_init_data, validate_login_widget

NONCE_COOKIE = "afisha_login_nonce"

router = APIRouter(tags=["Аккаунт"], dependencies=[accounts.PartnerKeyDep], responses={
    401: {"model": ErrorOut, "description": "Нужен вход"}, 403: {"model": ErrorOut, "description": "Не та роль"},
    422: {"model": ErrorOut}, 429: {"model": ErrorOut}})
admin_router = APIRouter(prefix="/admin", tags=["Аккаунты"], dependencies=[Depends(require_key_admin)], responses={
    401: {"model": ErrorOut, "description": "Нужен ключ администратора X-API-Key"}, 404: {"model": ErrorOut}, 422: {"model": ErrorOut}})


# ---------- схемы ----------

class TelegramLoginIn(BaseModel):
    init_data: str = Field("", max_length=4096, description="Строка initData мини-приложения Telegram")
    widget: dict[str, str | int] | None = Field(None, description="Данные кнопки «Войти через Telegram» (id, first_name, auth_date, hash…)")
    history: bool | None = Field(None, description="Согласие на историю посещений при первом входе; без него история выключена")


class UserOut(BaseModel):
    id: int
    display_name: str
    role: str
    role_label: str
    created_at: datetime
    history_enabled: bool
    consent_at: datetime | None = None
    history_retention_days: int


class LoginInfo(BaseModel):
    enabled: bool = Field(description="Вход настроен на сервере (задан токен бота)")
    bot_username: str


class MeOut(BaseModel):
    authenticated: bool
    user: UserOut | None = None
    login: LoginInfo


class ConsentIn(BaseModel):
    history_enabled: bool


class VisitIn(BaseModel):
    event_id: int = Field(ge=1, le=2**31 - 1)
    kind: Literal["view", "ticket"] = "view"


class VisitOut(BaseModel):
    id: int
    event_id: int
    kind: str
    viewed_at: datetime
    available: bool = Field(description="Карточка ещё есть в афише")
    title: str = ""
    city: str = ""
    venue: str = ""
    start_at: datetime | None = None
    status: str = ""


class HistoryPage(BaseModel):
    enabled: bool
    retention_days: int
    total: int
    items: list[VisitOut]


class SearchIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    filters: dict[str, Any] = Field(description="Условия, как в GET /events: q, city, venue, category, when, date_from, date_to, price_max, age, free, kids")


class SearchOut(BaseModel):
    id: int
    name: str
    filters: dict[str, Any]
    created_at: datetime


class OwnFeedbackOut(FeedbackOut):
    status_label: str = ""


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="Для чего ключ: сайт или приложение")


class KeyOut(BaseModel):
    id: int
    name: str
    prefix: str
    rate_limit: int = Field(description="Запросов в минуту")
    created_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    requests_total: int
    owner_user_id: int | None = None


class KeyCreated(KeyOut):
    key: str = Field(description="Сам ключ. Показывается один раз, в базе остаётся только хеш. Передаётся в заголовке X-Partner-Key")


class MinistryStats(BaseModel):
    events_published: int
    events_in_queue: int
    feedback_total: int
    feedback_by_status: dict[str, int]
    feedback_by_kind: dict[str, int]
    subscriptions_total: int
    keys: list[KeyOut]


class RoleIn(BaseModel):
    telegram_id: int = Field(ge=1, le=2**62, description="Номер пользователя Telegram")
    role: str = Field(description="user | organizer | moderator | ministry")
    display_name: str = Field("", max_length=100, description="Подпись для списка, пока человек не входил сам")


class UserAdminOut(BaseModel):
    id: int
    telegram_id: int
    display_name: str
    role: str
    role_label: str
    created_at: datetime
    last_seen_at: datetime | None = None
    history_enabled: bool
    signed_in: bool = Field(description="Человек уже входил сам; иначе роль выдана заранее и ждёт его")


class AdminKeyIn(KeyIn):
    rate_limit: int = Field(accounts.DEFAULT_KEY_LIMIT, ge=1, le=accounts.MAX_KEY_LIMIT)
    owner_telegram_id: int | None = Field(None, ge=1, description="Владелец ключа; пусто — ключ администратора без владельца")


class AdminKeyPatch(BaseModel):
    rate_limit: int = Field(ge=1, le=accounts.MAX_KEY_LIMIT)


def user_out(user: User) -> UserOut:
    return UserOut(id=user.id, display_name=user.display_name, role=user.role, role_label=accounts.role_label(user.role),
                   created_at=user.created_at, history_enabled=user.history_enabled, consent_at=user.consent_at,
                   history_retention_days=config.HISTORY_RETENTION_DAYS)


def me_out(user: User | None) -> MeOut:
    return MeOut(authenticated=user is not None, user=user_out(user) if user else None,
                 login=LoginInfo(enabled=bool(config.TELEGRAM_BOT_TOKEN), bot_username=config.TELEGRAM_BOT_USERNAME))


def _limit(request: Request) -> None:
    if not login_limiter.allow(RateLimiter.client_ip(request)):
        raise HTTPException(429, login_limiter.detail, headers={"Retry-After": "60"})


# ---------- вход и выход ----------

@router.post("/auth/telegram", response_model=MeOut, summary="Войти через Telegram (мини-приложение или кнопка входа)")
def login_telegram(payload: TelegramLoginIn, request: Request, response: Response, session: SessionDep) -> MeOut:
    """Подпись проверяется токеном бота, поэтому подделать вход нельзя. В ответе кука `afisha_session` (HttpOnly).
    Новый аккаунт создаётся без истории посещений: она включается только согласием (`history: true` или PATCH /me)."""
    _limit(request)
    if not config.TELEGRAM_BOT_TOKEN:
        raise HTTPException(503, "Вход через Telegram на этом сервере не настроен")
    if bool(payload.init_data) == bool(payload.widget):
        raise HTTPException(422, "Передайте либо init_data (мини-приложение), либо widget (кнопка входа)")
    tg = (parse_init_data(payload.init_data, config.TELEGRAM_BOT_TOKEN) if payload.init_data
          else validate_login_widget(payload.widget or {}, config.TELEGRAM_BOT_TOKEN))
    if tg is None:
        raise HTTPException(401, "Подпись Telegram неверна или устарела: откройте вход заново")
    user, _ = accounts.sign_in(session, tg, history=payload.history)
    accounts.set_login_cookie(response, accounts.start_session(session, user), embedded=bool(payload.init_data))
    return me_out(user)


@router.get("/auth/telegram/callback/{nonce}", include_in_schema=False)
def login_callback(nonce: Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{16,64}$")], request: Request, session: SessionDep) -> Response:
    """Куда Telegram возвращает браузер после кнопки «Войти». Одноразовый nonce из адреса должен совпасть с кукой, которую
    поставила наша страница: так чужая ссылка не войдёт в аккаунт злоумышленника за посетителя."""
    redirect = RedirectResponse("/account", status_code=303)
    redirect.delete_cookie(NONCE_COOKIE, path="/")
    if not login_limiter.allow(RateLimiter.client_ip(request)):
        redirect.headers["location"] = "/account?login=limit"
        return redirect
    tg = (validate_login_widget(dict(request.query_params), config.TELEGRAM_BOT_TOKEN)
          if config.TELEGRAM_BOT_TOKEN and request.cookies.get(NONCE_COOKIE) == nonce else None)
    if tg is None:
        redirect.headers["location"] = "/account?login=failed"
        return redirect
    user, _ = accounts.sign_in(session, tg)
    accounts.set_login_cookie(redirect, accounts.start_session(session, user))
    return redirect


@router.post("/auth/logout", status_code=204, summary="Выйти")
def logout(request: Request, session: SessionDep) -> Response:
    token = request.cookies.get(accounts.SESSION_COOKIE)
    if token:
        accounts.end_session(session, token)
    response = Response(status_code=204)
    accounts.clear_login_cookie(response)
    return response


# ---------- кабинет ----------

@router.get("/me", response_model=MeOut, summary="Кто вошёл")
def me(user: OptionalUser) -> MeOut:
    """Без входа отвечает 200 с `authenticated: false` и данными для кнопки входа: так странице не нужно ловить 401."""
    return me_out(user)


@router.patch("/me", response_model=MeOut, summary="Согласие на историю посещений")
def update_me(payload: ConsentIn, user: CurrentUser, session: SessionDep) -> MeOut:
    """Выключение стирает уже собранную историю: хранить её без согласия нельзя."""
    return me_out(accounts.set_history_consent(session, user, payload.history_enabled))


@router.delete("/me", status_code=204, summary="Удалить аккаунт и все данные о себе")
def delete_me(user: CurrentUser, session: SessionDep) -> Response:
    accounts.delete_account(session, user)
    response = Response(status_code=204)
    accounts.clear_login_cookie(response)
    return response


@router.get("/me/export", summary="Скачать все данные о себе (JSON)")
def export_me(user: CurrentUser, session: SessionDep) -> JSONResponse:
    return JSONResponse(jsonable_encoder(accounts.export_user_data(session, user)),
                        headers={"Content-Disposition": 'attachment; filename="afisha-my-data.json"'})


# ---------- история посещений ----------

@router.get("/me/history", response_model=HistoryPage, summary="История посещений")
def history(user: CurrentUser, session: SessionDep, limit: Annotated[int, Query(ge=1, le=200)] = 50,
            offset: Annotated[int, Query(ge=0, le=100_000)] = 0) -> HistoryPage:
    accounts.purge_history(session)
    items, total = accounts.list_history(session, user, limit=limit, offset=offset)
    return HistoryPage(enabled=user.history_enabled, retention_days=config.HISTORY_RETENTION_DAYS, total=total,
                       items=[VisitOut(**item) for item in items])


@router.post("/me/history", summary="Записать посещение (открыл карточку или перешёл по ссылке источника)")
def add_visit(payload: VisitIn, user: CurrentUser, session: SessionDep) -> dict:
    """Пишется только при согласии на историю и не чаще раза в сутки на карточку. Без согласия отвечает `recorded: false`."""
    return {"recorded": accounts.record_visit(session, user, payload.event_id, payload.kind)}


@router.delete("/me/history", summary="Очистить историю посещений")
def clear_history(user: CurrentUser, session: SessionDep) -> dict:
    return {"removed": accounts.clear_history(session, user)}


# ---------- сохранённые поиски ----------

def _search_out(item: SavedSearch) -> SearchOut:
    return SearchOut(id=item.id, name=item.name, filters=item.filters or {}, created_at=item.created_at)


@router.get("/me/searches", response_model=list[SearchOut], summary="Сохранённые поиски")
def list_searches(user: CurrentUser, session: SessionDep) -> list[SearchOut]:
    return [_search_out(s) for s in session.scalars(select(SavedSearch).where(SavedSearch.user_id == user.id).order_by(SavedSearch.id))]


@router.post("/me/searches", response_model=SearchOut, status_code=201, summary="Сохранить условия поиска")
def add_search(payload: SearchIn, user: CurrentUser, session: SessionDep) -> SearchOut:
    try:
        filters = accounts.clean_filters(payload.filters)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    count = session.scalar(select(func.count()).select_from(SavedSearch).where(SavedSearch.user_id == user.id)) or 0
    if count >= accounts.MAX_SAVED_SEARCHES:
        raise HTTPException(409, f"Уже {accounts.MAX_SAVED_SEARCHES} сохранённых поисков: удалите ненужный")
    item = SavedSearch(user_id=user.id, name=accounts.clean_name(payload.name, "Поиск"), filters=filters)
    session.add(item)
    session.commit()
    return _search_out(item)


@router.delete("/me/searches/{search_id}", status_code=204, summary="Удалить сохранённый поиск")
def delete_search(search_id: Annotated[int, Path(ge=1, le=2**31 - 1)], user: CurrentUser, session: SessionDep) -> Response:
    item = session.scalar(select(SavedSearch).where(SavedSearch.id == search_id, SavedSearch.user_id == user.id))
    if item is None:
        raise HTTPException(404, "Такого поиска нет")
    session.delete(item)
    session.commit()
    return Response(status_code=204)


# ---------- обращения организатора ----------

@router.get("/me/feedback", response_model=list[OwnFeedbackOut], summary="Мои обращения и заявки: статус и ответ модератора")
def own_feedback(user: CurrentUser, session: SessionDep) -> list[OwnFeedbackOut]:
    """Обращения, отправленные из этого аккаунта (форма `/feedback`, пока вы вошли). Статусы: Принято, В работе, Обработано."""
    return [OwnFeedbackOut(**item.model_dump(), status_label=STATUS_LABELS.get(item.status, item.status)) for item in list_own(session, user.id)]


# ---------- роль «Министерство» ----------

Ministry = Annotated[User, Depends(accounts.require_role("ministry"))]


@router.get("/me/api-keys", response_model=list[KeyOut], summary="Мои ключи для встраивания (Министерство)")
def my_keys(user: Ministry, session: SessionDep) -> list[KeyOut]:
    return [KeyOut(**accounts.key_view(k)) for k in session.scalars(select(ApiKey).where(ApiKey.owner_user_id == user.id).order_by(ApiKey.id))]


@router.post("/me/api-keys", response_model=KeyCreated, status_code=201, summary="Выпустить ключ только для чтения")
def create_my_key(payload: KeyIn, user: Ministry, session: SessionDep) -> KeyCreated:
    """Ключ отдаётся один раз. Передавайте его в заголовке `X-Partner-Key` при чтении афиши: у ключа свой лимит запросов в минуту
    вместо общего лимита по адресу, а записать что-либо им нельзя."""
    active = session.scalar(select(func.count()).select_from(ApiKey).where(ApiKey.owner_user_id == user.id, ApiKey.revoked_at.is_(None))) or 0
    if active >= accounts.MAX_KEYS_PER_USER:
        raise HTTPException(409, f"Уже {accounts.MAX_KEYS_PER_USER} действующих ключей: отзовите ненужный")
    key, token = accounts.create_api_key(session, name=accounts.clean_name(payload.name, "Ключ"), owner_user_id=user.id)
    return KeyCreated(**accounts.key_view(key), key=token)


@router.delete("/me/api-keys/{key_id}", status_code=204, summary="Отозвать свой ключ")
def revoke_my_key(key_id: Annotated[int, Path(ge=1, le=2**31 - 1)], user: Ministry, session: SessionDep) -> Response:
    key = session.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.owner_user_id == user.id))
    if key is None:
        raise HTTPException(404, "Такого ключа нет")
    accounts.revoke_api_key(session, key)
    return Response(status_code=204)


@router.get("/ministry/stats", response_model=MinistryStats, summary="Сводка для Министерства: афиша, обращения, ключи")
def ministry_stats(user: Ministry, session: SessionDep) -> MinistryStats:
    """Только счётчики, без текстов обращений и без данных людей."""
    published = session.scalar(select(func.count()).select_from(Event).where(or_(Event.publication.is_(None), Event.publication == "published"))) or 0
    queue = session.scalar(select(func.count()).select_from(Event).where(Event.publication == "draft")) or 0
    by_status = dict(session.execute(select(Feedback.status, func.count()).group_by(Feedback.status)).all())
    by_kind = dict(session.execute(select(Feedback.kind, func.count()).group_by(Feedback.kind)).all())
    subs = session.scalar(select(func.count()).select_from(Subscription)) or 0
    keys = session.scalars(select(ApiKey).where(ApiKey.owner_user_id == user.id).order_by(ApiKey.id))
    return MinistryStats(events_published=published, events_in_queue=queue, feedback_total=sum(by_status.values()),
                         feedback_by_status=by_status, feedback_by_kind=by_kind, subscriptions_total=subs,
                         keys=[KeyOut(**accounts.key_view(k)) for k in keys])


# ---------- администратор: роли и ключи ----------

def _user_admin(user: User) -> UserAdminOut:
    return UserAdminOut(id=user.id, telegram_id=user.telegram_id, display_name=user.display_name, role=user.role,
                        role_label=accounts.role_label(user.role), created_at=user.created_at, last_seen_at=user.last_seen_at,
                        history_enabled=user.history_enabled, signed_in=bool(user.display_name or user.last_seen_at))


@admin_router.get("/users", response_model=list[UserAdminOut], summary="Пользователи и роли")
def admin_users(session: SessionDep, role: Annotated[str | None, Query(pattern="^(user|organizer|moderator|ministry)$")] = None,
                q: Annotated[str | None, Query(max_length=100, description="Часть имени или номер Telegram")] = None,
                limit: Annotated[int, Query(ge=1, le=200)] = 50, offset: Annotated[int, Query(ge=0, le=100_000)] = 0) -> list[UserAdminOut]:
    stmt = select(User).order_by(User.id.desc())
    if role:
        stmt = stmt.where(User.role == role)
    if q:
        stmt = stmt.where(or_(User.display_name.ilike(f"%{q.strip()}%"), User.telegram_id == int(q) if q.strip().isdigit() else False))
    return [_user_admin(u) for u in session.scalars(stmt.limit(limit).offset(offset))]


@admin_router.post("/users", response_model=UserAdminOut, summary="Назначить роль по номеру Telegram",
                   description="Человек может ещё не входить: роль подождёт его первого входа. Действие пишется в журнал изменений.")
def admin_set_role(payload: RoleIn, session: SessionDep, actor: Annotated[str, Depends(require_key_admin)]) -> UserAdminOut:
    try:
        return _user_admin(accounts.set_role(session, payload.telegram_id, payload.role, actor, display_name=payload.display_name))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@admin_router.post("/users/{user_id}/revoke", response_model=UserAdminOut, summary="Отозвать доступ")
def admin_revoke(user_id: Annotated[int, Path(ge=1, le=2**31 - 1)], session: SessionDep,
                 actor: Annotated[str, Depends(require_key_admin)]) -> UserAdminOut:
    """Роль снова «житель», все входы закрыты, личные ключи отозваны. Аккаунт и история остаются у человека."""
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "Пользователь не найден")
    accounts.revoke_access(session, user, actor)
    return _user_admin(user)


@admin_router.get("/api-keys", response_model=list[KeyOut], summary="Все ключи для встраивания")
def admin_keys(session: SessionDep) -> list[KeyOut]:
    return [KeyOut(**accounts.key_view(k)) for k in session.scalars(select(ApiKey).order_by(ApiKey.id.desc()))]


@admin_router.post("/api-keys", response_model=KeyCreated, status_code=201, summary="Выпустить ключ с нужным лимитом")
def admin_create_key(payload: AdminKeyIn, session: SessionDep, actor: Annotated[str, Depends(require_key_admin)]) -> KeyCreated:
    owner = None
    if payload.owner_telegram_id is not None:
        owner = session.scalar(select(User).where(User.telegram_id == payload.owner_telegram_id))
        if owner is None:
            raise HTTPException(404, "Владельца с таким номером Telegram нет: сначала назначьте ему роль")
    key, token = accounts.create_api_key(session, name=accounts.clean_name(payload.name, "Ключ"),
                                         owner_user_id=owner.id if owner else None, rate_limit=payload.rate_limit)
    audit.record(session, actor, "api_key", None, {"key_id": key.id, "rate_limit": key.rate_limit, "owner": owner.id if owner else None})
    session.commit()
    return KeyCreated(**accounts.key_view(key), key=token)


@admin_router.patch("/api-keys/{key_id}", response_model=KeyOut, summary="Изменить лимит ключа")
def admin_patch_key(key_id: Annotated[int, Path(ge=1, le=2**31 - 1)], payload: AdminKeyPatch, session: SessionDep,
                    actor: Annotated[str, Depends(require_key_admin)]) -> KeyOut:
    key = session.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(404, "Ключа нет")
    audit.record(session, actor, "api_key", None, {"key_id": key.id, "rate_limit": [key.rate_limit, payload.rate_limit]})
    key.rate_limit = payload.rate_limit
    session.commit()
    return KeyOut(**accounts.key_view(key))


@admin_router.delete("/api-keys/{key_id}", status_code=204, summary="Отозвать ключ")
def admin_revoke_key(key_id: Annotated[int, Path(ge=1, le=2**31 - 1)], session: SessionDep,
                     actor: Annotated[str, Depends(require_key_admin)]) -> Response:
    key = session.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(404, "Ключа нет")
    audit.record(session, actor, "api_key", None, {"key_id": key.id, "revoked": True})
    accounts.revoke_api_key(session, key)
    return Response(status_code=204)
