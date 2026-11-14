"""Настройки из переменных окружения (и файла .env)."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

API_VERSION = "1.1.0"  # версия контракта API; в адресе — /api/v1, мажорная версия меняется только при несовместимых изменениях
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{(BASE_DIR / 'afisha.db').as_posix()}"
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
# Личные ключи модераторов: «имя1:ключ1,имя2:ключ2». В журнале изменений действие записывается на имя владельца ключа.
ADMIN_USERS = os.getenv("ADMIN_USERS", "")
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
# Имя бота без @: нужно кнопке «Войти через Telegram» на сайте. Домен сайта должен быть привязан к боту (BotFather: /setdomain).
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "BashSZbot").strip().lstrip("@")
# Аккаунты (docs/08): сколько дней живёт вход на сайте и сколько хранится история посещений.
SESSION_DAYS = max(1, int(os.getenv("SESSION_DAYS", "30") or 30))
HISTORY_RETENTION_DAYS = max(1, int(os.getenv("HISTORY_RETENTION_DAYS", "90") or 90))
# Адрес мини-приложения: Telegram открывает только HTTPS. По умолчанию берём PUBLIC_URL, если он https.
WEBAPP_URL = os.getenv("WEBAPP_URL") or (PUBLIC_URL if PUBLIC_URL.startswith("https://") else "")
# Если Telegram недоступен напрямую (блокировки), укажите прокси: http://127.0.0.1:10809 или socks5://host:port
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "")

# Обязательная подписка на канал для пользования ботом (бот должен быть администратором канала, иначе Telegram не
# покажет ему список подписчиков). По умолчанию выключено: в ТЗ такого условия нет, а оно удлиняет путь до результата.
# Включить: REQUIRED_CHANNEL=@sz_orenteam в .env.
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()
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
# Лимиты расхода на одного пользователя (ТЗ §6.5), в токенах (вход + выход); 0 = без лимита. Один ответ модели в прогоне
# 13.11.2026 — в среднем около 7 200 токенов. Сверх лимита отвечает режим правил с пояснением (§7.5).
LLM_USER_DAILY_TOKENS = _int_env("LLM_USER_DAILY_TOKENS", 150_000)  # на пользователя в сутки, ≈ 20 ответов модели
LLM_DIALOG_TOKENS = _int_env("LLM_DIALOG_TOKENS", 60_000)  # на диалог, ≈ 8 ответов модели
LLM_DIALOG_WINDOW_MIN = _int_env("LLM_DIALOG_WINDOW_MIN", 30) or 30  # диалог — вопросы за последние N минут

# Логи: json (по умолчанию, по строке на запрос) или text для разработки. В логи не попадают адреса и тексты запросов.
LOG_FORMAT = os.getenv("LOG_FORMAT", "json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
# Если задан, /metrics требует заголовок Authorization: Bearer <токен>. Без него метрики открыты: закройте их на прокси.
METRICS_TOKEN = os.getenv("METRICS_TOKEN", "")

def _float_env(name: str) -> float | None:
    try:
        return float(os.getenv(name, "")) if os.getenv(name, "").strip() else None
    except ValueError:
        return None


# Тарифы модели для расчёта стоимости диалога, рублей за 1 миллион токенов (ТЗ §9.2 №9). Не заданы: стоимость в трассах
# и отчётах не считается («тариф не задан»), число токенов считается всегда. Тариф подставьте из прайса своего провайдера.
LLM_PRICE_INPUT_RUB_PER_M = _float_env("LLM_PRICE_INPUT_RUB_PER_M")
LLM_PRICE_OUTPUT_RUB_PER_M = _float_env("LLM_PRICE_OUTPUT_RUB_PER_M")

AGENT_MODE = os.getenv("AGENT_MODE", "auto")  # auto | llm | rules
# Помощник работает на Google Gemini (ключ из AI Studio); без ключа отвечает режим правил.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
# Быстрая модель с вызовом функций. Старые (2.5) закрыты для новых ключей, алиас -latest тоже доступен не всем (404);
# список моделей своего ключа: client.models.list().
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
# Второй провайдер, бесплатный тариф Groq (API в формате OpenAI). Ключ: https://console.groq.com/keys. Без ключа не используется.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
# Список моделей ключа: GET {GROQ_BASE_URL}/models. gpt-oss-120b иногда отдаёт итог как вызов инструмента «json» (ошибка 400), 20b стабильнее.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
# Порядок провайдеров: если первый не ответил (квота, перегрузка, сеть), берётся следующий; потом режим правил.
LLM_PROVIDERS = [p.strip() for p in os.getenv("LLM_PROVIDERS", "gemini,groq").split(",") if p.strip()]
# Запасные модели через запятую: если основная вернула 429 (квота у каждой модели своя), 503 или 404, берётся следующая.
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.1-flash-lite,gemini-3.6-flash,gemini-3.8-flash,gemini-4-hydrogen").split(",") if m.strip()]
# Сколько секунд не трогать модель после отказа по квоте или перегрузке (суточная квота сбрасывается раз в сутки, поэтому не гоняем её зря).
GEMINI_COOLDOWN_SECONDS = int(os.getenv("GEMINI_COOLDOWN_SECONDS", "600"))

# Башкортостан живёт по UTC+5 без перехода на летнее время, поэтому фиксированного сдвига достаточно.
LOCAL_TZ = timezone(timedelta(hours=5))


def now_local() -> datetime:
    """Текущее местное время (Уфа) без tzinfo: так же хранятся даты событий."""
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)

# --- Проверка карточек перед публикацией и по расписанию (app/verify.py) ---
VERIFY_MIN_CONFIDENCE = float(os.getenv("VERIFY_MIN_CONFIDENCE", "0.55"))  # ниже: карточка остаётся в очереди модерации
VERIFY_STALE_DAYS = int(os.getenv("VERIFY_STALE_DAYS", "14"))  # опубликованные события перепроверяются раз в столько дней
VERIFY_LINK_TIMEOUT = float(os.getenv("VERIFY_LINK_TIMEOUT", "8"))  # секунд на проверку одной ссылки
