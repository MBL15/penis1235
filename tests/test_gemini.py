import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from google.genai import types

from app import agent, config

from .conftest import NOW


class FakeGemini:
    """Подменяет genai.Client: отдаёт заготовленные ответы и запоминает запросы."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.models = self

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def tool_call(name, args):
    return SimpleNamespace(name=name, args=args)


def model_reply(*, calls=(), text="", finish="STOP", blocked=False):
    parts = [types.Part(function_call=types.FunctionCall(name=c.name, args=c.args)) for c in calls] or [types.Part.from_text(text=text)]
    return SimpleNamespace(
        function_calls=list(calls),
        text=text,
        candidates=[SimpleNamespace(content=types.Content(role="model", parts=parts), finish_reason=SimpleNamespace(name=finish))],
        prompt_feedback=SimpleNamespace(block_reason="SAFETY") if blocked else None,
    )


def final(reply, ids, clarify=False, fenced=False):
    body = json.dumps({"reply": reply, "event_ids": ids, "needs_clarification": clarify}, ensure_ascii=False)
    return model_reply(text=f"```json\n{body}\n```" if fenced else body)


@pytest.fixture(autouse=True)
def gemini_mode(monkeypatch):
    monkeypatch.setattr(config, "AGENT_MODE", "llm")


@pytest.fixture
def seeded(add):
    puppets, _ = add("Кукольный спектакль «Три медведи»", datetime(2026, 11, 8, 12, 0), category="kids",
                     price_min=300, price_max=500, venue="Театр кукол")
    ballet, _ = add("Балет «Лебединое озеро»", datetime(2026, 11, 8, 18, 30), category="theatre",
                    price_min=600, price_max=3000, age_limit=6)
    return puppets, ballet


def test_tool_loop_with_float_numbers_and_hallucinated_id(session, seeded):
    puppets, _ = seeded
    client = FakeGemini(
        # Gemini присылает целые как float: 1000.0 не должно просочиться в фильтры и ответ
        model_reply(calls=[tool_call("search_events", {
            "with_children": True, "max_price": 1000.0, "date_from": "2026-11-08", "date_to": "2026-11-08"})]),
        final("Подойдёт кукольный спектакль.", [float(puppets.id), 9999]),
    )
    result = agent.ask(session, "куда пойти в воскресенье с ребёнком до 1000 рублей", client=client, now=NOW)

    assert (result.mode, result.provider) == ("llm", "gemini")
    assert [e.id for e in result.events] == [puppets.id]  # выдуманный 9999 отброшен, 12.0 принят как 12
    assert result.filters["price_max"] == 1000 and result.filters["kids"] is True

    first, second = client.calls
    assert first["model"] == config.GEMINI_MODEL
    settings = first["config"]
    assert "2026-11-07" in settings.system_instruction and "суббота" in settings.system_instruction
    assert settings.automatic_function_calling.disable is True
    assert "Описывай только факты" in settings.system_instruction and "развёрнуто" in settings.system_instruction
    assert settings.max_output_tokens >= 4096
    assert {d.name for d in settings.tools[0].function_declarations} == {"search_events", "get_event"}
    answer = second["contents"][-1]
    assert answer.role == "user"
    payload = answer.parts[0].function_response.response["result"]
    assert payload["total"] == 1 and "Три медведи" in payload["events"][0]["title"]


def test_numbers_sent_as_strings_still_filter_by_price_and_age(session, seeded):
    puppets, ballet = seeded
    client = FakeGemini(
        model_reply(calls=[tool_call("search_events", {"max_price": "400", "child_age": "5", "limit": "3"})]),
        final("Кукольный спектакль до 400 ₽.", [puppets.id]),
    )
    result = agent.ask(session, "дёшево, ребёнку 5 лет", client=client, now=NOW)
    assert (result.filters["price_max"], result.filters["age"]) == (400, 5)
    payload = client.calls[1]["contents"][-1].parts[0].function_response.response["result"]
    assert [e["id"] for e in payload["events"]] == [puppets.id]  # балет от 600 ₽ отсеян: строка «400» не сломала сравнение
    assert agent._filters_from_tool_input({"max_price": "дорого", "limit": "x"}).price_max is None


def test_fenced_json_is_parsed(session, seeded):
    puppets, _ = seeded
    client = FakeGemini(final("Вот что нашёл.", [puppets.id], fenced=True))
    result = agent.ask(session, "что есть", client=client, now=NOW)
    assert result.provider == "gemini" and [e.id for e in result.events] == []  # id не из поиска: отброшен


def test_bad_tool_input_is_returned_to_the_model(session, seeded):
    client = FakeGemini(
        model_reply(calls=[tool_call("search_events", {"date_from": "не-дата"})]),
        final("Уточните дату.", [], clarify=True),
    )
    result = agent.ask(session, "что в субботу", client=client, now=NOW)
    assert result.needs_clarification and result.provider == "gemini"
    assert "error" in client.calls[1]["contents"][-1].parts[0].function_response.response


def test_history_roles_are_mapped_and_start_with_user(session, seeded):
    client = FakeGemini(final("Ок", []))
    history = [{"role": "assistant", "content": "Привет"}, {"role": "user", "content": "про детей"},
               {"role": "assistant", "content": "Сколько лет?"}]
    agent.ask(session, "5 лет", history, client=client, now=NOW)
    sent = client.calls[0]["contents"]
    assert [c.role for c in sent] == ["user", "model", "user"]
    assert sent[-1].parts[0].text == "5 лет"


@pytest.mark.parametrize("failure", [
    model_reply(blocked=True),
    model_reply(text="", finish="SAFETY"),
    RuntimeError("нет сети"),
])
def test_block_or_api_failure_falls_back_to_rules(session, seeded, failure):
    result = agent.ask(session, "в воскресенье с ребёнком", client=FakeGemini(failure), now=NOW)
    assert result.mode == "rules" and result.provider == "" and result.events


def test_endless_tool_loop_is_cut_off(session, seeded):
    loop = [model_reply(calls=[tool_call("search_events", {})]) for _ in range(agent.MAX_STEPS)]
    result = agent.ask(session, "в воскресенье", client=FakeGemini(*loop), now=NOW)
    assert result.mode == "rules"


def test_model_is_used_only_with_a_key_and_not_in_rules_mode(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert not agent.llm_enabled()
    monkeypatch.setattr(config, "GEMINI_API_KEY", "g-key")
    assert agent.llm_enabled()
    monkeypatch.setattr(config, "AGENT_MODE", "rules")
    assert not agent.llm_enabled()  # «rules» запрещает вызывать модель, даже если ключ есть


def test_rules_mode_never_calls_a_model(session, seeded, monkeypatch):
    monkeypatch.setattr(config, "AGENT_MODE", "rules")
    client = FakeGemini()  # любой вызов упал бы: ответов нет
    result = agent.ask(session, "в воскресенье", client=client, now=NOW)
    assert result.mode == "rules" and client.calls == []
