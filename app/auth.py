"""Доступ к методам, которые меняют данные: заголовок X-API-Key."""
import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Request

from . import config
from .security import RateLimiter, auth_limiter


def require_admin(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> str:
    key = config.ADMIN_API_KEY
    if not key:
        raise HTTPException(503, "Приём данных отключён: на сервере не задан ADMIN_API_KEY")
    if len(key) < config.ADMIN_KEY_MIN_LENGTH:
        raise HTTPException(503, f"Приём данных отключён: ADMIN_API_KEY короче {config.ADMIN_KEY_MIN_LENGTH} символов")
    ip = RateLimiter.client_ip(request)
    if auth_limiter.blocked(ip):  # подбор ключа: после 10 неудач с адреса минуту не принимаем даже верный ключ
        raise HTTPException(429, auth_limiter.detail, headers={"Retry-After": "60"})
    # Сравниваем байты: compare_digest на строках с не-ASCII символами бросает TypeError.
    if not x_api_key or not secrets.compare_digest(x_api_key.encode("utf-8"), key.encode("utf-8")):
        auth_limiter.allow(ip)
        raise HTTPException(401, "Неверный или отсутствующий заголовок X-API-Key")
    return "admin"
