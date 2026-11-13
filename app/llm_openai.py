"""Клиент API в формате OpenAI Chat Completions (Groq и другие совместимые сервисы).

Только стандартная библиотека: новых зависимостей и колёс для сервера не нужно. Прокси берётся из HTTPS_PROXY окружения,
как и у клиента Gemini. Ошибка HTTP превращается в LlmHttpError с кодом ответа: по нему агент решает, пробовать ли другую модель."""
import json
import urllib.error
import urllib.request

from . import config


class LlmHttpError(Exception):
    def __init__(self, code: int, text: str = ""):
        super().__init__(f"HTTP {code}: {text[:300]}")
        self.code = code


class OpenAICompatClient:
    """chat(model, messages, tools) -> словарь ответа API. Подменяется в тестах."""

    def __init__(self, api_key: str, base_url: str, timeout: float = 30.0):
        self.api_key, self.base_url, self.timeout = api_key, base_url.rstrip("/"), timeout

    def chat(self, *, model: str, messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.3,
             max_tokens: int = 2048) -> dict:
        body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            body.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                     "User-Agent": "afisha-agent/1.0"},  # без своего User-Agent часть сервисов за Cloudflare отвечает 403
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LlmHttpError(exc.code, exc.read().decode("utf-8", "replace")) from exc


def to_openai_tools(tools: list[dict]) -> list[dict]:
    """Описания инструментов агента (name, description, input_schema) → формат OpenAI."""
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in tools]


def groq_client() -> OpenAICompatClient:
    return OpenAICompatClient(config.GROQ_API_KEY, config.GROQ_BASE_URL)
