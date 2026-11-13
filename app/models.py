from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
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
    # Все источники, подтвердившие событие: [{"name": "...", "url": "..."}]
    sources: Mapped[list] = mapped_column(JSON, default=list)
    dedup_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    search_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, onupdate=now_local)
