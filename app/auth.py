"""Доступ к методам, которые меняют данные: заголовок X-API-Key.

Ключи бывают двух видов:
  * ADMIN_API_KEY: один общий ключ, действия записываются в журнал от имени «admin»;
  * ADMIN_USERS="имя1:ключ1,имя2:ключ2": у каждого модератора свой ключ, и в журнале видно, кто что изменил.
Каждый ключ не короче ADMIN_KEY_MIN_LENGTH символов, иначе он не принимается."""
import re
import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Request

from . import config
from .security import RateLimiter, auth_limiter

_ACTOR = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def admin_keys() -> tuple[dict[str, str], bool]:
    """({ключ: имя модератора}, были ли ключи короче допустимого). Читается при каждом запросе: настройки можно менять на лету."""
    entries = []
    if config.ADMIN_API_KEY:
        entries.append(("admin", config.ADMIN_API_KEY))
    for part in re.split(r"[;,]", config.ADMIN_USERS):
        name, _, key = part.strip().partition(":")
        if name.strip() and key.strip():
            entries.append((name.strip(), key.strip()))
    keys: dict[str, str] = {}
    too_short = False
    for name, key in entries:
        if not _ACTOR.fullmatch(name):
            continue  # имя попадает в журнал: только безопасные символы
        if len(key) < config.ADMIN_KEY_MIN_LENGTH:
            too_short = True
            continue
        keys[key] = name
    return keys, too_short


def require_admin(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> str:
    """Проверяет ключ и возвращает имя модератора для журнала изменений."""
    keys, too_short = admin_keys()
    if not keys:
        if too_short:
            raise HTTPException(503, f"Приём данных отключён: ключ короче {config.ADMIN_KEY_MIN_LENGTH} символов (ADMIN_API_KEY)")
        raise HTTPException(503, "Приём данных отключён: на сервере не задан ADMIN_API_KEY")
    ip = RateLimiter.client_ip(request)
    if auth_limiter.blocked(ip):  # подбор ключа: после 10 неудач с адреса минуту не принимаем даже верный ключ
        raise HTTPException(429, auth_limiter.detail, headers={"Retry-After": "60"})
    actor = None
    if x_api_key:
        provided = x_api_key.encode("utf-8")  # байты: compare_digest на строках с не-ASCII символами бросает TypeError
        for key, name in keys.items():  # перебираем все ключи, не прерываясь на первом совпадении
            if secrets.compare_digest(provided, key.encode("utf-8")):
                actor = name
    if actor is None:
        auth_limiter.allow(ip)
        raise HTTPException(401, "Неверный или отсутствующий заголовок X-API-Key")
    return actor
