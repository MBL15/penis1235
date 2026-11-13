"""Второй провайдер: сервис с API в формате OpenAI (Groq). Клиент подменён, сеть не нужна."""
import json
import urllib.error
from datetime import datetime
from io import BytesIO

import pytest

from app import agent, config
from app.llm_openai import LlmHttpError, OpenAICompatClient, to_openai_tools
from app.agent_tools import TOOLS
from app.tracing import get_trace

from .conftest import NOW
from .test_gemini import ApiError, FakeGemini


class FakeGroq:
    """Отдаёт заготовленные ответы chat completions и запоминает запросы."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def chat(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs, ensure_ascii=False)))  # снимок: список сообщений дальше растёт
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def completion(content="", calls=(), tokens=(100, 20), finish="stop"):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": a}}
                                 for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": message, "finish_reason": finish}], "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]}}


def answer(reply, ids):
    return completion(json.dumps({"reply": reply, "event_ids": ids, "needs_clarification": False}, ensure_ascii=False))


@pytest.fixture(autouse=True)
def llm_mode(monkeypatch):
    monkeypatch.setattr(config, "AGENT_MODE", "llm")
    monkeypatch.setattr(config, "LLM_PROVIDERS", ["gemini", "groq"])
    agent._cooldown.clear()
    yield
    agent._cooldown.clear()


@pytest.fixture
def seeded(add):
    puppets, _ = add("Кукольный спектакль «Три медведи»", datetime(2026, 11, 8, 12, 0), category="kids", price_min=300, price_max=500)
    return puppets


def test_tool_loop_in_openai_format(session, seeded):
    groq = FakeGroq(
        completion(calls=[("search_events", json.dumps({"with_children": True, "max_price": 1000.0,
                                                         "date_from": "2026-11-08", "date_to": "2026-11-08"}))], tokens=(5000, 40)),
        answer("Подойдёт кукольный спектакль.", [seeded.id, 9999]),
    )
    result = agent.ask(session, "куда пойти в воскресенье с ребёнком до 1000 рублей", openai_client=groq, now=NOW)
    assert (result.mode, result.provider) == ("llm", "groq")
    assert [e.id for e in result.events] == [seeded.id]  # выдуманный 9999 отброшен, как и у Gemini
    assert result.filters["price_max"] == 1000 and result.filters["kids"] is True

    first, second = groq.calls
    assert first["model"] == config.GROQ_MODEL and first["messages"][0]["role"] == "system"
    assert {t["function"]["name"] for t in first["tools"]} == {t["name"] for t in TOOLS}
    assert [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "tool"]
    assert second["messages"][3]["tool_call_id"] == "call_0" and "Три медведи" in second["messages"][3]["content"]
    trace = get_trace(session, result.trace_id)
    assert (trace.provider, trace.model, trace.input_tokens, trace.output_tokens) == ("groq", config.GROQ_MODEL, 5100, 60)


def test_broken_tool_arguments_do_not_crash(session, seeded):
    groq = FakeGroq(completion(calls=[("search_events", "{не json")]), answer("Ничего не нашёл.", []))
    result = agent.ask(session, "что есть", openai_client=groq, now=NOW)
    assert result.provider == "groq" and result.reply == "Ничего не нашёл."


def test_groq_answers_when_gemini_is_out_of_quota(session, seeded, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_FALLBACK_MODELS", [])
    gemini = FakeGemini(ApiError(429, "RESOURCE_EXHAUSTED"))
    groq = FakeGroq(answer("Есть спектакль.", []))
    result = agent.ask(session, "что в воскресенье", client=gemini, openai_client=groq, now=NOW)
    assert (result.mode, result.provider, result.reply) == ("llm", "groq", "Есть спектакль.")
    trace = get_trace(session, result.trace_id)
    assert "gemini" in trace.flags["provider_skipped"] and "fallback_reason" not in trace.flags


def test_both_providers_down_means_rules_and_both_reasons_are_recorded(session, seeded, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_FALLBACK_MODELS", [])
    gemini = FakeGemini(ApiError(503, "UNAVAILABLE"))
    groq = FakeGroq(LlmHttpError(429, "rate limit"))
    result = agent.ask(session, "что в воскресенье", client=gemini, openai_client=groq, now=NOW)
    assert result.mode == "rules"
    reason = get_trace(session, result.trace_id).flags["fallback_reason"]
    assert "gemini:" in reason and "groq:" in reason and "429" in reason


def test_provider_order_and_availability(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    assert not agent.llm_enabled()
    monkeypatch.setattr(config, "GROQ_API_KEY", "gsk-test")
    assert agent.llm_enabled() and agent._providers() == ["groq"]
    monkeypatch.setattr(config, "GEMINI_API_KEY", "g-key")
    monkeypatch.setattr(config, "LLM_PROVIDERS", ["groq", "gemini"])
    assert agent._providers() == ["groq", "gemini"]  # порядок задаёт LLM_PROVIDERS
    monkeypatch.setattr(config, "AGENT_MODE", "rules")
    assert not agent.llm_enabled()


def test_rules_mode_never_calls_the_second_provider(session, seeded, monkeypatch):
    monkeypatch.setattr(config, "AGENT_MODE", "rules")
    groq = FakeGroq()
    assert agent.ask(session, "что в воскресенье", openai_client=groq, now=NOW).mode == "rules" and not groq.calls


def test_http_client_sends_openai_request_and_maps_errors(monkeypatch):
    seen = {}

    class Response(BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, headers=dict(request.header_items()), body=json.loads(request.data), timeout=timeout)
        return Response(json.dumps({"choices": []}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatClient("gsk-secret", "https://api.example.com/v1/")
    client.chat(model="m", messages=[{"role": "user", "content": "привет"}], tools=to_openai_tools(TOOLS))
    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer gsk-secret" and "afisha" in seen["headers"]["User-agent"]
    assert seen["body"]["tool_choice"] == "auto" and seen["body"]["messages"][0]["content"] == "привет"

    def deny(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, BytesIO(b'{"error":"rate"}'))

    monkeypatch.setattr("urllib.request.urlopen", deny)
    with pytest.raises(LlmHttpError) as err:
        client.chat(model="m", messages=[])
    assert err.value.code == 429 and "rate" in str(err.value)


def test_short_rate_limit_is_waited_out_and_long_one_is_not(monkeypatch):
    from app.llm_openai import retry_after

    assert retry_after(None, "Please try again in 6.4275s. Need more") == pytest.approx(6.4275)
    assert retry_after(None, "Please try again in 19h43m8.3s") is None or retry_after(None, "try again in 1m5s") == 65
    assert retry_after({"retry-after": "3"}, "") == 3 and retry_after(None, "нет подсказки") is None

    sleeps, calls = [], []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    def flaky(request, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 429, "x", {}, BytesIO(b'{"error":{"message":"Please try again in 2s."}}'))

        class Ok(BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return Ok(b'{"choices": []}')

    monkeypatch.setattr("urllib.request.urlopen", flaky)
    assert OpenAICompatClient("k", "https://x/v1").chat(model="m", messages=[]) == {"choices": []}
    assert len(calls) == 2 and sleeps == [2.5]

    calls.clear()
    monkeypatch.setattr("urllib.request.urlopen", lambda r, timeout: (_ for _ in ()).throw(
        urllib.error.HTTPError(r.full_url, 429, "x", {}, BytesIO(b'{"error":{"message":"Please try again in 40s."}}'))))
    with pytest.raises(LlmHttpError):  # 40 с ждать нельзя: ответ правилами лучше минуты молчания
        OpenAICompatClient("k", "https://x/v1").chat(model="m", messages=[])
