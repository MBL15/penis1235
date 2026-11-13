from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .config import LOCAL_TZ

StatusName = Literal["scheduled", "rescheduled", "sold_out", "cancelled"]
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
    price_min: int | None = Field(None, ge=0, le=MAX_PRICE, description="0 = бесплатно, null = цена неизвестна")
    price_max: int | None = Field(None, ge=0, le=MAX_PRICE)
    age_limit: int | str | None = Field(None, description="число или строка вида «12+»")
    kids_friendly: bool | None = None
    url: str = Field("", max_length=500)
    image_url: str = Field("", max_length=500)
    status: StatusName = "scheduled"
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
    price_min: int | None
    price_max: int | None
    price_text: str
    is_free: bool
    age_limit: int
    kids_friendly: bool
    url: str
    image_url: str
    status: str
    status_label: str
    is_past: bool
    sources: list[SourceRef]
    source_count: int
    is_demo: bool


class EventList(BaseModel):
    items: list[EventOut]
    total: int
    limit: int
    offset: int


class IngestResult(BaseModel):
    outcome: Literal["created", "merged", "unchanged"]
    event: EventOut


class BulkIngestResult(BaseModel):
    created: int
    merged: int
    unchanged: int
    events: list[EventOut]


class StatusPatch(BaseModel):
    status: StatusName


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


class AskOut(BaseModel):
    reply: str
    events: list[EventOut]
    needs_clarification: bool
    mode: Literal["llm", "rules"]
    provider: str = Field("", description="Какая модель отвечала: gemini; пусто в режиме правил")
    filters: dict = Field(default_factory=dict)
