"""Настройки из переменных окружения (и файла .env)."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{(BASE_DIR / 'afisha.db').as_posix()}"
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
# Короткий ключ подбирается перебором: приём данных включается только с ключом не короче этой длины.
ADMIN_KEY_MIN_LENGTH = 24
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/")
# Пометка для пользователей: сервис не выдаётся за официальный (ТЗ §13). Текст согласуется с заказчиком (Приложение В),
# поэтому его можно заменить переменной DISCLAIMER, не меняя код. Статические страницы (сайт, виджет) держат свою копию.
DISCLAIMER = os.getenv("DISCLAIMER") or (
    "Неофициальный сервис: не является сервисом Министерства культуры Республики Башкортостан и не связан с ним. "
    "Данные собраны из открытых источников, перед визитом уточняйте их у организатора."
)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Адрес мини-приложения: Telegram открывает только HTTPS. По умолчанию берём PUBLIC_URL, если он https.
WEBAPP_URL = os.getenv("WEBAPP_URL") or (PUBLIC_URL if PUBLIC_URL.startswith("https://") else "")
# Если Telegram недоступен напрямую (блокировки), укажите прокси: http://127.0.0.1:10809 или socks5://host:port
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "")

# Обязательная подписка на канал для пользования ботом (бот должен быть администратором канала, иначе Telegram не
# покажет ему список подписчиков). Пустое значение отключает проверку.
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@sz_orenteam").strip()
REQUIRED_CHANNEL_TITLE = os.getenv("REQUIRED_CHANNEL_TITLE", "SpaceZ").strip()

# Сервис стоит за прокси/платформой (Render, Railway, nginx): лимит запросов считаем по X-Forwarded-For, а не по адресу прокси.
TRUST_PROXY = os.getenv("TRUST_PROXY", "") == "1"


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, "") or default))
    except ValueError:
        return default


# Имена хоста, на которые отвечает сервис (через запятую). Пусто = любые; в продакшене укажите свой домен,
# чтобы запросы с подставным заголовком Host отклонялись.
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]
# Общий лимит запросов к API с одного адреса в минуту.
API_RATE_LIMIT = _int_env("API_RATE_LIMIT", 300) or 300
# Бюджет ИИ: сколько запросов к модели идёт одновременно и сколько всего за сутки (0 = без суточного лимита).
# Сверх бюджета отвечает режим правил, сервис при этом не падает и не копит очередь.
LLM_MAX_CONCURRENCY = _int_env("LLM_MAX_CONCURRENCY", 4) or 4
LLM_DAILY_LIMIT = _int_env("LLM_DAILY_LIMIT", 3000)
LLM_TIME_BUDGET = _int_env("LLM_TIME_BUDGET", 45) or 45  # секунд на весь ответ вместе с вызовами инструментов

AGENT_MODE = os.getenv("AGENT_MODE", "auto")  # auto | llm | rules
# Помощник работает на Google Gemini (ключ из AI Studio); без ключа отвечает режим правил.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
# Быстрая модель с вызовом функций. Старые (2.5) закрыты для новых ключей, алиас -latest тоже доступен не всем (404);
# список моделей своего ключа: client.models.list().
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

# Башкортостан живёт по UTC+5 без перехода на летнее время, поэтому фиксированного сдвига достаточно.
LOCAL_TZ = timezone(timedelta(hours=5))


def now_local() -> datetime:
    """Текущее местное время (Уфа) без tzinfo: так же хранятся даты событий."""
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)
