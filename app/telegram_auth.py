"""Проверка подписи Telegram Mini App (initData): так API узнаёт, чей это чат, не принимая номер на веру.

Мини-приложение присылает строку initData с подписью HMAC-SHA256, ключ которой выводится из токена бота
(https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app). Подделать её без токена нельзя."""
import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl

MAX_AGE_SECONDS = 24 * 3600  # чтение (список своих подписок): мини-приложение бывает открыто часами
# Изменения и вход: перехваченная строка initData не должна сутки управлять чужими подписками или давать 30-дневную куку
# входа (ревью 14.11.2026, Б-6). Мини-приложение, открытое дольше часа, переоткрывается из бота.
WRITE_MAX_AGE_SECONDS = 3600


def validate_init_data(init_data: str, bot_token: str, *, now: float | None = None, max_age: int = MAX_AGE_SECONDS) -> int | None:
    """Номер пользователя Telegram (он же номер личного чата с ботом) или None, если подпись неверна или устарела."""
    if not init_data or not bot_token or len(init_data) > 4096:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    given = pairs.pop("hash", "")
    if not given:
        return None
    check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given):
        return None
    try:
        issued = int(pairs.get("auth_date", "0"))
        user = json.loads(pairs.get("user", "{}"))
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError):
        return None
    if (now if now is not None else time.time()) - issued > max_age:
        return None
    return user_id


@dataclass(frozen=True)
class TelegramUser:
    """Кто вошёл: номер и имя, больше ничего из Telegram не берём (ни username, ни фото, ни телефон)."""

    id: int
    name: str


def _display_name(first: object, last: object) -> str:
    name = " ".join(str(part).strip() for part in (first, last) if part and str(part).strip())
    return re.sub(r"[\x00-\x1f\x7f]", "", name)[:100]


def parse_init_data(init_data: str, bot_token: str, *, now: float | None = None, max_age: int = WRITE_MAX_AGE_SECONDS) -> TelegramUser | None:
    """Как validate_init_data, но возвращает ещё и имя: для аккаунта в мини-приложении."""
    user_id = validate_init_data(init_data, bot_token, now=now, max_age=max_age)
    if user_id is None:
        return None
    try:
        user = json.loads(dict(parse_qsl(init_data, keep_blank_values=True)).get("user", "{}"))
    except ValueError:
        user = {}
    return TelegramUser(user_id, _display_name(user.get("first_name"), user.get("last_name")) or f"Пользователь {user_id}")


LOGIN_MAX_AGE_SECONDS = 3600  # подпись кнопки входа живёт час: вход происходит сразу после нажатия


def validate_login_widget(fields: Mapping[str, object], bot_token: str, *, now: float | None = None) -> TelegramUser | None:
    """Проверка данных кнопки «Войти через Telegram» (https://core.telegram.org/widgets/login#checking-authorization).

    Ключ здесь другой, чем у мини-приложения: SHA-256 от токена бота. Подделать подпись без токена нельзя."""
    if not bot_token or not fields or len(fields) > 20:
        return None
    pairs = {str(k): str(v) for k, v in fields.items() if len(str(k)) <= 32 and len(str(v)) <= 1024}
    given = pairs.pop("hash", "")
    if not given:
        return None
    check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    expected = hmac.new(hashlib.sha256(bot_token.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given):
        return None
    try:
        issued = int(pairs.get("auth_date", "0"))
        user_id = int(pairs["id"])
    except (ValueError, KeyError):
        return None
    if user_id <= 0 or (now if now is not None else time.time()) - issued > LOGIN_MAX_AGE_SECONDS:
        return None
    return TelegramUser(user_id, _display_name(pairs.get("first_name"), pairs.get("last_name")) or f"Пользователь {user_id}")


def make_login_widget_data(user_id: int, bot_token: str, *, auth_date: int | None = None, first_name: str = "Тест") -> dict[str, str]:
    """Корректно подписанные данные кнопки входа: нужны тестам, на сервере не используются."""
    fields = {"id": str(user_id), "first_name": first_name, "username": "tester",
              "auth_date": str(auth_date if auth_date is not None else int(time.time()))}
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    fields["hash"] = hmac.new(hashlib.sha256(bot_token.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    return fields


def make_init_data(user_id: int, bot_token: str, *, auth_date: int | None = None, first_name: str = "T") -> str:
    """Собирает корректно подписанную строку: нужна тестам и проверке вручную, на сервере не используется."""
    from urllib.parse import urlencode

    fields = {"auth_date": str(auth_date if auth_date is not None else int(time.time())),
              "user": json.dumps({"id": user_id, "first_name": first_name}, separators=(",", ":"), ensure_ascii=False)}
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)
