from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import now_local


class Base(DeclarativeBase):
    pass


class Event(Base):
    """Одно событие. Даты хранятся в местном времени (Уфа, UTC+5) без tzinfo."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(32), default="other", index=True)
    city: Mapped[str] = mapped_column(String(64), default="Уфа", index=True)
    venue: Mapped[str] = mapped_column(String(200), default="")
    address: Mapped[str] = mapped_column(String(300), default="")
    start_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Серия показов: start_at — известный показ (первый или ближайший), series_until — конец серии. Остальные даты
    # источник не сообщает, поэтому для поиска по дате серия считается только в день start_at.
    series_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)  # от источника или из окончания в тот же день
    language: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)  # ba | ru | tt | en…, только если назван явно
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)  # координаты, если источник их отдаёт (schema.org geo)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_min: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0 = бесплатно, None = неизвестно
    price_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    age_limit: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)  # None = «не указан», 0 = «0+»
    kids_friendly: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    url: Mapped[str] = mapped_column(String(500), default="")
    image_url: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(16), default="scheduled", index=True)
    # Публикация (ТЗ §6.1): published — видно всем, draft — только модератору (очередь модерации). Отмена — это status.
    publication: Mapped[str | None] = mapped_column(String(12), default="published", index=True)
    venue_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)  # нормализованная площадка: поиск и справочник
    # Все источники, подтвердившие событие: [{"name": "...", "url": "..."}]
    sources: Mapped[list] = mapped_column(JSON, default=list)
    dedup_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    search_text: Mapped[str] = mapped_column(Text, default="")
    # Поля ТЗ §8.1, добавленные миграцией 0003
    normalized_title: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)  # название для поиска и дедупликации
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)  # метки: джаз, дети, бесплатно, башкирский язык
    source_type: Mapped[str | None] = mapped_column(String(12), nullable=True)  # manual | import | api | page | rss
    source_hash: Mapped[str | None] = mapped_column(String(40), nullable=True)  # хеш записи первого источника: идемпотентность
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # когда карточку проверяли последний раз
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)  # уверенность 0–1 (app/verify.py)
    review_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)  # почему карточка в очереди модерации
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, onupdate=now_local)


class Subscription(Base):
    """Подписка на новые события: только номер чата Telegram и фильтры. Ничего другого о человеке не хранится (ТЗ §10)."""

    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("chat_id", "city", "category", name="uq_subscription"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    city: Mapped[str] = mapped_column(String(64), default="")  # пусто = любой город
    category: Mapped[str] = mapped_column(String(32), default="")  # пусто = любая категория
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)  # события, добавленные позже, считаются новыми
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Аккаунт-владелец (docs/08): подписка из бота и с сайта остаётся одной записью, у старых подписок поле пусто
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)


class Feedback(Base):
    """Обращение через форму обратной связи или заявка организатора (ТЗ §6.3). Хранится только то, что человек написал сам:
    адрес, устройство и прочие данные посетителя не сохраняются. Срок хранения: app.feedback.RETENTION_DAYS."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="other")  # organizer | correction | question | other
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text)
    contact: Mapped[str] = mapped_column(String(200), default="")  # необязательно: как ответить
    message_hash: Mapped[str] = mapped_column(String(40), default="", index=True)  # для отсева повторов
    status: Mapped[str] = mapped_column(String(12), default="new", index=True)  # new | in_progress | processed
    processed_by: Mapped[str] = mapped_column(String(100), default="")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Автор-аккаунт и ответ модератора (docs/08): у организатора есть список своих обращений со статусом и ответом
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # когда автору ушло уведомление об ответе


class RawRecord(Base):
    """Сырая запись источника ровно в том виде, как пришла (ТЗ §6.1): по ней нормализацию можно переиграть без повторного сбора.

    Одна и та же запись источника хранится один раз (источник + хеш содержимого); если источник прислал её снова,
    обновляется только last_seen_at. Изменилась запись у источника, появляется новая строка: так видна история."""

    __tablename__ = "raw_records"
    __table_args__ = (UniqueConstraint("source", "payload_hash", name="uq_raw_source_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(100), index=True)
    payload_hash: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, index=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # событие, в которое запись превратилась
    outcome: Mapped[str] = mapped_column(String(16), default="")  # created | merged | unchanged | rejected
    error: Mapped[str] = mapped_column(Text, default="")  # почему отклонена


class AuditLog(Base):
    """Журнал изменений (ТЗ §6.1, §8.2): кто, когда и что изменил. changes: {поле: [было, стало]}."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now_local, index=True)
    actor: Mapped[str] = mapped_column(String(100), index=True)  # admin, имя модератора, sync:kassir.ru, import:файл…
    action: Mapped[str] = mapped_column(String(32))  # create | update | merge | status | publication | edit | import
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    request_id: Mapped[str] = mapped_column(String(64), default="")


class AgentTrace(Base):
    """Трасса одного ответа агента (ТЗ §7.4): запрос, план, вызовы инструментов с аргументами, события-основания, задержка,
    токены и стоимость. По ней можно проверить, откуда взялся каждый факт. Идентификатор случайный и служит ссылкой на трассу.
    Срок хранения: app.tracing.RETENTION_DAYS."""

    __tablename__ = "agent_traces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, index=True)
    request_id: Mapped[str] = mapped_column(String(64), default="")
    mode: Mapped[str] = mapped_column(String(8), default="rules")  # llm | rules
    provider: Mapped[str] = mapped_column(String(16), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    query: Mapped[str] = mapped_column(Text, default="")
    history_turns: Mapped[int] = mapped_column(Integer, default=0)
    plan: Mapped[dict] = mapped_column(JSON, default=dict)  # как поняли запрос: условия поиска и поправки
    steps: Mapped[list] = mapped_column(JSON, default=list)  # вызовы инструментов
    event_ids: Mapped[list] = mapped_column(JSON, default=list)  # показанные события: основания ответа
    reply: Mapped[str] = mapped_column(Text, default="")
    needs_clarification: Mapped[bool] = mapped_column(Boolean, default=False)
    flags: Mapped[dict] = mapped_column(JSON, default=dict)  # injection, offtopic, fallback, ungrounded…
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_rub: Mapped[float | None] = mapped_column(Float, nullable=True)  # None: тариф не задан


class PendingAction(Base):
    """Действие, которое агент предложил выполнить и которое меняет данные: подписка, перенос подписки, обращение (ТЗ §6.5).
    Выполняется только после явного подтверждения человеком кнопкой; без него по истечении срока просто устаревает."""

    __tablename__ = "agent_actions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    kind: Mapped[str] = mapped_column(String(20))  # subscribe | move_subscription | feedback
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")  # что будет сделано, текст пишет сервер, а не модель
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | confirmed | cancelled
    trace_id: Mapped[str] = mapped_column(String(32), default="")


class User(Base):
    """Аккаунт (docs/08). Хранится только то, без чего он не работает: номер Telegram, имя оттуда же, роль и согласие на историю.
    Телефон, почта и пароли не запрашиваются. Сайт работает и без аккаунта."""

    __tablename__ = "users"
    # Номера не используются повторно после удаления аккаунта: на номер аккаунта ссылаются записи журнала изменений (user:<номер>)
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100), default="")  # из Telegram; пусто, пока человек не входил (роль выдана заранее)
    role: Mapped[str] = mapped_column(String(12), default="user", index=True)  # user | organizer | moderator | ministry
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    history_enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # история посещений: только по согласию
    consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # когда согласие дано


class UserSession(Base):
    """Вход на сайте. В базе только хеш токена: кража базы не даёт войти. Сам токен лежит в куке HttpOnly."""

    __tablename__ = "user_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class VisitHistory(Base):
    """История посещений: открыл карточку (view) или перешёл по ссылке источника (ticket). Пишется только при согласии,
    хранится app.accounts.HISTORY_RETENTION_DAYS дней, стирается кнопкой."""

    __tablename__ = "visit_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    event_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(8), default="view")  # view | ticket
    viewed_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, index=True)


class SavedSearch(Base):
    """Сохранённые условия поиска: те же фильтры, что и у подписок и GET /events."""

    __tablename__ = "saved_searches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(80))
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)


class ApiKey(Base):
    """Ключ для встраивания (роль «Министерство»): только чтение афиши, свой лимит запросов в минуту. В базе хеш, сам ключ
    показывается один раз при создании."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(12), default="")  # начало ключа, чтобы отличать ключи в списке
    name: Mapped[str] = mapped_column(String(80), default="")
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    rate_limit: Mapped[int] = mapped_column(Integer, default=600)  # запросов в минуту
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    requests_total: Mapped[int] = mapped_column(Integer, default=0)


class SyncRun(Base):
    """Один запуск сбора из внешнего источника (app/scheduler.py): когда, что получили и чем кончилось."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(64), index=True)  # имя задачи из data/schedule.json
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="running")  # running | ok | failed
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    merged: Mapped[int] = mapped_column(Integer, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    published: Mapped[int] = mapped_column(Integer, default=0)  # из очереди проверки в выдачу
    queued: Mapped[int] = mapped_column(Integer, default=0)  # осталось в очереди модерации
    vanished: Mapped[int] = mapped_column(Integer, default=0)  # пропало из источника
    revived: Mapped[int] = mapped_column(Integer, default=0)  # вернулось в источник
    error: Mapped[str] = mapped_column(String(400), default="")


class BotDialog(Base):
    """Последние реплики диалога с ботом и события из прошлого ответа (для «ещё», «дешевле», «похожее»): переживают
    перезапуск бота. Хранится не дольше app.dialogs.RETENTION_HOURS после последней реплики (ТЗ §10: минимум данных)."""

    __tablename__ = "bot_dialogs"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    history: Mapped[list] = mapped_column(JSON, default=list)  # [{role, content}], не больше app.dialogs.MAX_TURNS
    event_ids: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, index=True)


class LoginRequest(Base):
    """Вход на сайт через бота (без виджета telegram.org и привязки домена в BotFather): сайт выдаёт одноразовый код и ссылку
    t.me/<бот>?start=login_<код>, человек подтверждает вход кнопкой в боте, страница забирает вход. В базе только хеши кода и
    секрета браузера: забрать вход может лишь тот браузер, который его начал. Живёт accounts.LOGIN_TTL."""

    __tablename__ = "login_requests"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    display_name: Mapped[str] = mapped_column(String(100), default="")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

