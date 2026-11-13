"""Единый формат ошибок API (ТЗ §6.3): {"error": {"code", "message", "details", "request_id"}}.

code — короткий машинный код, message — понятное сообщение по-русски, details — список подробностей (для ошибок
проверки: поле и что с ним не так), request_id — идентификатор запроса для обращения в поддержку."""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .observability import request_id_var

CODES = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
    409: "conflict", 411: "length_required", 413: "payload_too_large", 422: "validation_error", 429: "rate_limited",
    500: "internal_error", 503: "unavailable",
}
DEFAULT_MESSAGES = {  # когда сообщение не задано в коде (например, неизвестный адрес): по-русски, а не «Not Found»
    404: "Такой страницы или метода нет",
    405: "Этот метод для адреса не поддерживается",
}


def error_body(status: int, message: str, code: str | None = None, details: list | None = None) -> dict:
    return {"error": {
        "code": code or CODES.get(status, "error"),
        "message": message,
        "details": details or [],
        "request_id": request_id_var.get(),
    }}


def error_response(status: int, message: str, code: str | None = None, details: list | None = None,
                   headers: dict | None = None) -> JSONResponse:
    return JSONResponse(error_body(status, message, code, details), status_code=status, headers=headers)


def _validation_message(error: dict) -> str:
    kind, ctx = error.get("type", ""), error.get("ctx") or {}
    messages = {
        "missing": "обязательное поле",
        "int_parsing": "нужно целое число",
        "int_from_float": "нужно целое число",
        "float_parsing": "нужно число",
        "bool_parsing": "нужно true или false",
        "date_parsing": "нужна дата в формате ГГГГ-ММ-ДД",
        "date_from_datetime_parsing": "нужна дата в формате ГГГГ-ММ-ДД",
        "datetime_parsing": "нужны дата и время в формате ГГГГ-ММ-ДДTЧЧ:ММ",
        "datetime_from_date_parsing": "нужны дата и время в формате ГГГГ-ММ-ДДTЧЧ:ММ",
        "string_type": "нужна строка",
        "string_pattern_mismatch": "недопустимое значение",
        "json_invalid": "тело запроса не является корректным JSON",
        "dict_type": "ожидался объект",
        "model_attributes_type": "ожидался объект",
        "list_type": "ожидался список",
        "too_long": f"слишком много элементов (не больше {ctx.get('max_length', '?')})",
        "too_short": f"слишком мало элементов (не меньше {ctx.get('min_length', '?')})",
        "string_too_short": f"слишком коротко (не меньше {ctx.get('min_length', '?')} символов)",
        "string_too_long": f"слишком длинно (не больше {ctx.get('max_length', '?')} символов)",
        "greater_than_equal": f"не меньше {ctx.get('ge', '?')}",
        "greater_than": f"больше {ctx.get('gt', '?')}",
        "less_than_equal": f"не больше {ctx.get('le', '?')}",
        "less_than": f"меньше {ctx.get('lt', '?')}",
        "literal_error": f"допустимые значения: {ctx.get('expected', '?')}",
        "enum": f"допустимые значения: {ctx.get('expected', '?')}",
    }
    if kind in messages:
        return messages[kind]
    message = str(error.get("msg", "некорректное значение"))
    return message.removeprefix("Value error, ")  # сообщения наших проверок уже по-русски


def humanize_validation(errors: list[dict]) -> tuple[str, list[dict]]:
    """Ошибки pydantic → (общее сообщение, [{field, message}]) по-русски."""
    details = []
    where = ""
    for error in errors:
        loc = [str(p) for p in error.get("loc", ())]
        where = where or (loc[0] if loc else "")
        field = ".".join(loc[1:] if loc and loc[0] in ("query", "body", "path", "header") else loc)
        details.append({"field": field or "(тело запроса)", "message": _validation_message(error)})
    summary = {"query": "Некорректные параметры запроса", "path": "Некорректный адрес", "body": "Некорректное тело запроса"}
    return summary.get(where, "Некорректные данные запроса"), details


def install(app: FastAPI) -> None:
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) and exc.detail not in ("Not Found", "Method Not Allowed") else None
        return error_response(exc.status_code, message or DEFAULT_MESSAGES.get(exc.status_code, "Ошибка запроса"),
                              headers=getattr(exc, "headers", None))

    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        message, details = humanize_validation(exc.errors())
        return error_response(422, message, "validation_error", details)

    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, invalid_request)
