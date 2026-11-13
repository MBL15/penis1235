"""Кеширование ответов API (ТЗ §6.3): ETag и Cache-Control, условные запросы получают 304 без тела."""
import hashlib
import json

from fastapi import Request, Response
from fastapi.encoders import jsonable_encoder


def _matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    wanted = etag.removeprefix("W/")
    return header.strip() == "*" or any(tag.strip().removeprefix("W/") == wanted for tag in header.split(","))


def cached_html(request: Request, page: str, *, max_age: int, status_code: int = 200) -> Response:
    """HTML-страница с ETag и Cache-Control, как и ответы API."""
    body = page.encode("utf-8")
    etag = f'W/"{hashlib.sha1(body).hexdigest()[:20]}"'
    headers = {"ETag": etag, "Cache-Control": f"public, max-age={max_age}"}
    if status_code == 200 and _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    if status_code != 200:
        headers = {"Cache-Control": "no-cache"}
    return Response(content=body, status_code=status_code, media_type="text/html; charset=utf-8", headers=headers)


def cached_json(request: Request, payload, *, max_age: int) -> Response:
    """JSON-ответ с ETag (хеш тела) и Cache-Control: public. Если клиент уже держит эту версию, отдаёт 304."""
    body = json.dumps(jsonable_encoder(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    etag = f'W/"{hashlib.sha1(body).hexdigest()[:20]}"'
    headers = {"ETag": etag, "Cache-Control": f"public, max-age={max_age}"}
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)
