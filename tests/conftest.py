import os
import sys
from datetime import datetime
from pathlib import Path

# Окружение задаём до импорта приложения: тесты не должны трогать рабочую БД и платный API.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AGENT_MODE"] = "rules"
# Рабочий .env (токен бота, ключ Gemini, адрес мини-приложения, прокси) не должен влиять на тесты.
for _name in ("WEBAPP_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_PROXY", "ADMIN_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY",
              "REQUIRED_CHANNEL"):  # подписка на канал по умолчанию включена: в тестах бота её включаем явно
    os.environ[_name] = ""
os.environ["PUBLIC_URL"] = "http://localhost:8000"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import config  # noqa: E402
from app.db import get_session  # noqa: E402
from app.ingest import upsert_event  # noqa: E402
from app.main import app  # noqa: E402
from app.security import api_limiter, ask_limiter, auth_limiter, llm_budget, user_llm_budget  # noqa: E402
from app.models import Base  # noqa: E402
from app.schemas import EventIn  # noqa: E402

NOW = datetime(2026, 11, 7, 15, 0)  # суббота, 15:00


@pytest.fixture(autouse=True)
def _fresh_user_llm_budget():
    """Лимиты расхода модели на пользователя живут в памяти процесса: каждый тест начинает с нуля."""
    user_llm_budget.reset()
    yield
    user_llm_budget.reset()


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def client(session):
    def override():
        yield session

    app.dependency_overrides[get_session] = override
    from app.feedback import feedback_global_limiter, feedback_limiter

    for limiter in (ask_limiter, api_limiter, auth_limiter, feedback_limiter, feedback_global_limiter):
        limiter.hits.clear()
    llm_budget.reset()
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def admin_key(monkeypatch):
    key = "test-key-" + "k" * 32  # короткие ключи приложение не принимает
    monkeypatch.setattr(config, "ADMIN_API_KEY", key)
    return {"X-API-Key": key}


@pytest.fixture
def add(session):
    """add('Название', datetime(...), price_min=..., ...) → (Event, outcome)"""

    def _add(title, start_at, source="t1", **kw):
        return upsert_event(session, EventIn(title=title, start_at=start_at, source=source, **kw))

    return _add
