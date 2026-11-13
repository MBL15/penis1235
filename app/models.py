from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint
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
    price_min: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0 = бесплатно, None = неизвестно
    price_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    age_limit: Mapped[int] = mapped_column(Integer, default=0)
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
