"""Проверка подписи Telegram Mini App (initData): так API узнаёт, чей это чат, не принимая номер на веру.

Мини-приложение присылает строку initData с подписью HMAC-SHA256, ключ которой выводится из токена бота
(https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app). Подделать её без токена нельзя."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_SECONDS = 24 * 3600  # старше суток — считаем перехваченной


def validate_init_data(init_data: str, bot_token: str, *, now: float | None = None) -> int | None:
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
    if (now if now is not None else time.time()) - issued > MAX_AGE_SECONDS:
        return None
    return user_id


def make_init_data(user_id: int, bot_token: str, *, auth_date: int | None = None) -> str:
    """Собирает корректно подписанную строку: нужна тестам и проверке вручную, на сервере не используется."""
    from urllib.parse import urlencode

    fields = {"auth_date": str(auth_date if auth_date is not None else int(time.time())),
              "user": json.dumps({"id": user_id, "first_name": "T"}, separators=(",", ":"))}
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)
