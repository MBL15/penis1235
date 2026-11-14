from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .config import LOCAL_TZ

StatusName = Literal["scheduled", "rescheduled", "sold_out", "cancelled"]
PublicationName = Literal["published", "draft"]
MAX_PRICE = 10_000_000  # ₽; заодно защищает от переполнения INTEGER в базе


class SourceRef(BaseModel):
    name: str
    url: str = ""


def _http_url(value: str) -> str:
    """Ссылка либо пустая, либо http(s): «javascript:» и прочие схемы не должны попасть в базу и к потребителям API."""
    value = (value or "").strip()
    if value and not value.lower().startswith(("http://", "https://")):
        raise ValueError("ссылка должна начинаться с http:// или https://")
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("в ссылке не должно быть пробелов и управляющих символов")
    return value


class EventIn(BaseModel):
    """Событие, как его присылает источник (парсер, организатор, сайт министерства)."""

    title: str = Field(min_length=2, max_length=300)
    description: str = Field("", max_length=20_000)
    category: str | None = Field(None, max_length=64, description="slug (theatre, concert, …) или русское название")
    city: str | None = Field(None, max_length=64)
    venue: str = Field("", max_length=200)
    address: str = Field("", max_length=300)
    start_at: datetime
    end_at: datetime | None = None
    series_until: datetime | None = Field(
        None, description="Конец серии показов: start_at — известный показ, остальные даты серии источник не сообщает"
    )
    duration_min: int | None = Field(None, ge=1, le=20_160, description="Длительность, минут; если не указана, выводится из времени окончания в тот же день")
    language: str | None = Field(None, max_length=32, description="Язык мероприятия (ba, ru, tt, en или название), только если указан явно")
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    price_min: int | None = Field(None, ge=0, le=MAX_PRICE, description="0 = бесплатно, null = цена неизвестна")
    price_max: int | None = Field(None, ge=0, le=MAX_PRICE)
    age_limit: int | str | None = Field(None, description="число или строка вида «12+»")
    kids_friendly: bool | None = None
    url: str = Field("", max_length=500)
    image_url: str = Field("", max_length=500)
    status: StatusName = "scheduled"
    publication: PublicationName = Field(
        "published", description="published — видно всем, draft — черновик для модерации (виден только администратору)")
    source: str = Field(min_length=1, max_length=100, description="Имя источника, например bashopera.ru")
    source_url: str = Field("", max_length=500)

    model_config = {"json_schema_extra": {"examples": [{
        "title": "Концерт симфонического оркестра", "start_at": "2026-11-21T19:00:00+05:00", "city": "Уфа",
        "venue": "Башкирская государственная филармония", "category": "concert", "price_min": 300, "price_max": 1200,
        "age_limit": "6+", "url": "https://example.ru/events/1", "source": "example.ru"}]}}

    @field_validator("title", "venue", "address", "source")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("title", "source")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("поле не может состоять из пробелов")
        return value

    @field_validator("url", "image_url", "source_url")
    @classmethod
    def _safe_url(cls, value: str) -> str:
        return _http_url(value)

    @field_validator("age_limit")
    @classmethod
    def _age(cls, value: int | str | None) -> int | str | None:
        if isinstance(value, str) and len(value) > 16:
            raise ValueError("возрастное ограничение: не больше 16 символов")
        if isinstance(value, int) and not 0 <= value <= 99:
            raise ValueError("возрастное ограничение: от 0 до 99")
        return value

    @field_validator("start_at", "end_at", "series_until")
    @classmethod
    def _to_local_naive(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            return value.astimezone(LOCAL_TZ).replace(tzinfo=None)
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "EventIn":
        """Источники ошибаются в мелочах: чиним, а не отклоняем всю запись."""
        if self.end_at is not None and self.end_at < self.start_at:
            self.end_at = None
        # Серия имеет смысл, только если заканчивается позже дня известного показа и событие не идёт непрерывно.
        if self.series_until is not None and (self.end_at is not None or self.series_until.date() <= self.start_at.date()):
            self.series_until = None
        if self.price_min is not None and self.price_max is not None and self.price_max < self.price_min:
            self.price_min, self.price_max = self.price_max, self.price_min
        return self


class EventOut(BaseModel):
    id: int
    title: str
    description: str
    category: str
    category_label: str
    city: str
    venue: str
    address: str
    start_at: datetime
    end_at: datetime | None
    series_until: datetime | None = Field(None, description="Конец серии показов; даты между start_at и этим днём неизвестны")
    duration_min: int | None = Field(None, description="Длительность в минутах; null, если неизвестна")
    language: str | None = Field(None, description="Код языка (ba, ru, tt, en…); null, если язык не указан")
    language_label: str = ""
    lat: float | None = None
    lon: float | None = None
    price_min: int | None
    price_max: int | None
    price_text: str
    is_free: bool
    age_limit: int | None = Field(None, description="возрастное ограничение; null — источник не указал")
    kids_friendly: bool
    url: str
    image_url: str
    status: str
    status_label: str
    publication: str = Field("published", description="published | draft (черновик виден только администратору)")
    is_past: bool
    sources: list[SourceRef]
    source_count: int
    is_demo: bool
    tags: list[str] = Field(default_factory=list, description="метки: джаз, дети, бесплатно, башкирский язык")
    source_type: str | None = Field(None, description="manual | import | api | page | rss — откуда пришла первая запись")
    verified_at: datetime | None = Field(None, description="когда карточку последний раз проверяли; null — не проверялась")
    confidence: float | None = Field(None, description="уверенность 0–1: ссылка, дата, полнота, число источников")
    review_reason: str | None = Field(None, description="почему карточка в очереди модерации (только у черновиков)")


class PlanIn(BaseModel):
    """Параметры планировщика «Выходной день»."""

    day: date = Field(description="день, на который строится план")
    window_from: str = Field("10:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="свободное окно: с (ЧЧ:ММ)")
    window_to: str = Field("22:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="свободное окно: по (ЧЧ:ММ)")
    budget: int | None = Field(None, ge=0, le=1_000_000, description="бюджет на всех, рублей; пусто — без ограничения")
    adults: int = Field(1, ge=1, le=10)
    children: list[int] = Field(default_factory=list, max_length=8, description="возраст каждого ребёнка, лет")
    city: str | None = Field(None, max_length=64)
    max_travel_min: int | None = Field(40, ge=0, le=240, description="сколько минут на один переезд; пусто — без ограничения")
    events: int = Field(3, ge=2, le=4, description="сколько событий хотите (2–4)")
    category: str | None = Field(None, max_length=32)

    @field_validator("children")
    @classmethod
    def _ages(cls, value: list[int]) -> list[int]:
        if any(not 0 <= age <= 17 for age in value):
            raise ValueError("возраст ребёнка: от 0 до 17 лет")
        return value


class PlanItemOut(BaseModel):
    event: "EventOut"
    starts_at: datetime
    ends_at: datetime
    end_estimated: bool = Field(description="окончание оценено по типичной длительности: источник его не сообщил")
    travel_min: int = Field(description="оценка дороги от предыдущего события, минут; у первого 0")
    cost: int = Field(description="стоимость на всех, рублей")
    price_known: bool


class PlanOut(BaseModel):
    feasible: bool
    message: str
    suggestions: list[str] = Field(description="что изменить, чтобы план получился, от самого малого изменения")
    constraints: list[str]
    requested_events: int
    items: list[PlanItemOut]
    total_cost: int
    total_travel_min: int


class SubscriptionIn(BaseModel):
    city: str = Field("", max_length=64, description="город из справочника; пусто — вся республика")
    category: str = Field("", max_length=32, description="категория из справочника; пусто — любая")


class SubscriptionOut(BaseModel):
    id: int
    city: str
    category: str
    label: str = Field(description="человекочитаемо: «Уфа · Концерты»")


class EventList(BaseModel):
    items: list[EventOut]
    hint: str | None = Field(None, description="при пустой выдаче: что искали и какое условие можно ослабить")
    total: int
    limit: int
    offset: int


class CityOut(BaseModel):
    name: str
    count: int = Field(description="Сколько актуальных событий в городе")


class CategoryOut(BaseModel):
    slug: str
    label: str
    count: int


class VenueOut(BaseModel):
    name: str = Field(description="Название площадки (самое частое написание); его можно передать в параметр venue")
    city: str
    count: int
    names: list[str] = Field(default_factory=list, description="Другие написания той же площадки в источниках")


class IngestResult(BaseModel):
    outcome: Literal["created", "merged", "unchanged"]
    event: EventOut


class RejectedRecord(BaseModel):
    row: int = Field(description="Номер записи в файле или пачке (с 1; в CSV первая строка данных — 2)")
    title: str = ""
    reasons: list[str] = Field(description="Что не так, по-русски: «поле: причина»")


class BulkIngestResult(BaseModel):
    """Отчёт о загрузке: добавлено, обновлено, отклонено и почему (ТЗ §6.1). merged — прежнее имя поля updated."""

    created: int
    updated: int = 0
    merged: int
    unchanged: int
    rejected: list[RejectedRecord] = Field(default_factory=list)
    total: int = 0
    events: list[EventOut] = Field(default_factory=list)


class StatusPatch(BaseModel):
    status: StatusName


class EventPatch(BaseModel):
    """Правка карточки модератором: меняются только переданные поля. null очищает необязательное поле (цену, конец)."""

    model_config = {"extra": "forbid"}

    title: str | None = Field(None, min_length=2, max_length=300)
    description: str | None = Field(None, max_length=20_000)
    category: str | None = Field(None, max_length=64)
    city: str | None = Field(None, max_length=64)
    venue: str | None = Field(None, max_length=200)
    address: str | None = Field(None, max_length=300)
    start_at: datetime | None = None
    end_at: datetime | None = None
    series_until: datetime | None = None
    duration_min: int | None = Field(None, ge=1, le=20_160)
    language: str | None = Field(None, max_length=32)
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    price_min: int | None = Field(None, ge=0, le=MAX_PRICE)
    price_max: int | None = Field(None, ge=0, le=MAX_PRICE)
    age_limit: int | str | None = None
    kids_friendly: bool | None = None
    url: str | None = Field(None, max_length=500)
    image_url: str | None = Field(None, max_length=500)

    @field_validator("url", "image_url")
    @classmethod
    def _safe_url(cls, value: str | None) -> str | None:
        return _http_url(value) if value is not None else None

    @field_validator("age_limit")
    @classmethod
    def _age(cls, value: int | str | None) -> int | str | None:
        if isinstance(value, str) and len(value) > 16:
            raise ValueError("возрастное ограничение: не больше 16 символов")
        if isinstance(value, int) and not 0 <= value <= 99:
            raise ValueError("возрастное ограничение: от 0 до 99")
        return value

    @field_validator("start_at", "end_at", "series_until")
    @classmethod
    def _to_local_naive(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            return value.astimezone(LOCAL_TZ).replace(tzinfo=None)
        return value


class PublicationPatch(BaseModel):
    publication: PublicationName


class AuditEntryOut(BaseModel):
    id: int
    at: datetime
    actor: str = Field(description="Кто изменил: admin, имя модератора, sync:источник, import:файл")
    action: str = Field(description="create | update | merge | status | publication | edit | delete")
    event_id: int | None = None
    changes: dict = Field(default_factory=dict, description="{поле: [было, стало]}")
    request_id: str = ""


class ImportIn(BaseModel):
    """Файл с событиями (JSON или CSV), прочитанный браузером: так загрузка работает без multipart."""

    filename: str = Field(min_length=3, max_length=200, description="Имя файла: по расширению .json или .csv выбирается разбор")
    content: str = Field(max_length=5_000_000, description="Текст файла")
    publish: bool = Field(True, description="false: новые события сохраняются черновиками для модерации")


class SyncRunOut(BaseModel):
    id: int
    job: str
    started_at: datetime
    finished_at: datetime | None
    status: str = Field(description="running | ok | failed")
    fetched: int
    created: int
    merged: int
    unchanged: int
    skipped: int
    published: int = Field(description="из очереди проверки в выдачу")
    queued: int = Field(description="осталось в очереди модерации")
    vanished: int = Field(description="пропало из источника")
    revived: int = Field(description="вернулось в источник")
    error: str


class AdminSessionOut(BaseModel):
    actor: str
    via: str = "key"  # key | account
    can_manage_users: bool = True  # роли и ключи Министерства выдаёт только владелец ключа администратора


class ErrorBody(BaseModel):
    code: str = Field(description="Машинный код: not_found, validation_error, unauthorized, rate_limited, …")
    message: str = Field(description="Сообщение для человека, по-русски")
    details: list[dict] = Field(default_factory=list, description="Подробности; для validation_error: [{field, message}]")
    request_id: str = Field("", description="Идентификатор запроса (он же заголовок X-Request-ID), по нему ищут запись в логе")


class ErrorOut(BaseModel):
    """Единый формат всех ошибок API."""

    error: ErrorBody

    model_config = {"json_schema_extra": {"examples": [{"error": {
        "code": "validation_error", "message": "Некорректные параметры запроса",
        "details": [{"field": "limit", "message": "не больше 100"}], "request_id": "9f1c0b7e5d3a4c2e8b6a1d0f3e5c7a92"}}]}}


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    # Развёрнутый ответ модели бывает длинным: принимаем с запасом, а в модель уходит обрезанная история (agent.py).
    content: str = Field(max_length=12_000)


class AskIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)
    # Номера в пределах INTEGER базы: иначе SQLite не может подставить число в запрос и отвечает 500
    context_event_ids: list[Annotated[int, Field(ge=1, le=2**31 - 1)]] = Field(
        default_factory=list, max_length=10,
        description="Номера событий из прошлого ответа: нужны для уточнений «а ещё», «в другой день», «что-нибудь похожее»")


class AskOut(BaseModel):
    reply: str
    events: list[EventOut]
    needs_clarification: bool
    mode: Literal["llm", "rules"]
    provider: str = Field("", description="Какой провайдер модели отвечал: gemini или groq; пусто в режиме правил")
    filters: dict = Field(default_factory=dict)
    trace_id: str = Field("", description="Ссылка на трассу ответа: GET /api/v1/traces/{trace_id} и страница /trace/{trace_id}")
    pending_actions: list["PendingActionOut"] = Field(
        default_factory=list, description="Предложенные действия (подписка, обращение): выполняются только после подтверждения")


class PendingActionOut(BaseModel):
    id: str
    kind: str = Field(description="subscribe | move_subscription | feedback")
    summary: str = Field(description="Что будет сделано; текст пишет сервер")
    expires_at: str


class ActionResult(BaseModel):
    ok: bool
    message: str


class TraceOut(BaseModel):
    """Трасса ответа агента: по ней можно проверить, откуда взялся каждый факт (ТЗ §7.4)."""

    id: str
    created_at: datetime
    mode: str = Field(description="llm — отвечала модель, rules — режим правил")
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    query: str
    history_turns: int = 0
    plan: dict = Field(default_factory=dict, description="Как поняли запрос: условия поиска, поправки, неучтённое")
    steps: list[dict] = Field(default_factory=list, description="Вызовы инструментов: tool, args, ok, event_ids, total, ms")
    event_ids: list[int] = Field(default_factory=list, description="События, показанные в ответе (основания)")
    events: list[dict] = Field(default_factory=list, description="Те же события: id и название")
    reply: str = ""
    needs_clarification: bool = False
    flags: dict = Field(default_factory=dict, description="injection, offtopic, fallback_reason, ungrounded_numbers, …")
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_rub: float | None = Field(None, description="null: тариф не задан в настройках")
    cost_note: str = ""

AskOut.model_rebuild()
