"""Режим без ИИ (запасной): ответы по правилам. Сценарии с моделью Gemini: tests/test_gemini.py."""
from datetime import datetime

import pytest

from app import agent

from .conftest import NOW


@pytest.fixture
def seeded(add):
    puppets, _ = add("Кукольный спектакль «Три медведи»", datetime(2026, 11, 8, 12, 0), category="kids",
                     price_min=300, price_max=500, venue="Театр кукол")
    ballet, _ = add("Балет «Лебединое озеро»", datetime(2026, 11, 8, 18, 30), category="theatre",
                    price_min=600, price_max=3000, age_limit=6)
    return puppets, ballet


def test_rules_mode_answers_the_headline_question(session, seeded):
    result = agent.ask(session, "куда пойти в воскресенье с ребёнком до 1000 рублей", now=NOW)
    assert result.mode == "rules" and result.provider == ""
    assert [e.title for e in result.events] == ["Кукольный спектакль «Три медведи»"]
    assert "воскресенье" in result.reply and "1000" in result.reply


def test_rules_mode_asks_to_clarify_vague_questions(session, seeded):
    result = agent.ask(session, "что посоветуешь?", now=NOW)
    assert result.needs_clarification and not result.events


def test_rules_mode_relaxes_conditions_and_says_so(session, seeded):
    result = agent.ask(session, "в воскресенье до 100 рублей", now=NOW)
    assert result.events, "после ослабления цены должны найтись события"
    assert "Ослабил" in result.reply and "цене" in result.reply


def test_rules_mode_relaxes_only_what_was_asked_and_keeps_category(session, seeded, add):
    add("Джазовый вечер", datetime(2026, 11, 13, 20, 0), category="concert", price_min=800)
    result = agent.ask(session, "джаз в воскресенье", now=NOW)  # в воскресенье джаза нет, ближайший — через неделю
    assert [e.title for e in result.events] == ["Джазовый вечер"]
    assert "по дате" in result.reply
    assert "цене" not in result.reply and "возрасту" not in result.reply and "категории" not in result.reply


def test_rules_mode_remembers_the_question_across_a_clarification(session, seeded):
    first = agent.ask(session, "с ребёнком", now=NOW)  # возраст «с ребёнком» уже распознан, дата не нужна
    history = [{"role": "user", "content": "с ребёнком"}, {"role": "assistant", "content": first.reply}]
    second = agent.ask(session, "в воскресенье", history, now=NOW)
    assert second.filters["kids"] is True and second.filters["date_from"] == "2026-11-08"


def test_without_a_gemini_key_the_rules_answer(session, seeded, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "AGENT_MODE", "auto")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert not agent.llm_enabled()
    assert agent.ask(session, "в воскресенье", now=NOW).mode == "rules"
