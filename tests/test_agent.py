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


# ---------- ТЗ §2, §7.3, Приложение А: честные ответы без ИИ ----------

def test_missing_keyword_is_reported_instead_of_substituting_other_concerts(session, add):
    add("Концерт «Цвети, республика моя!»", datetime(2026, 11, 10, 14, 0), city="Салават", category="concert", price_min=350)
    result = agent.ask(session, "Есть ли в Салавате джаз на следующей неделе?", now=NOW)
    assert "событий со словом «джаз» не нашлось" in result.reply  # раньше отвечало «Нашёл 1 событие» про не-джаз
    assert [e.title for e in result.events] == ["Концерт «Цвети, республика моя!»"]


def test_existing_keyword_is_found(session, add):
    add("Джазовый вечер", datetime(2026, 11, 10, 20, 0), city="Салават", category="concert", price_min=800)
    add("Эстрадный концерт", datetime(2026, 11, 10, 14, 0), city="Салават", category="concert", price_min=350)
    result = agent.ask(session, "Есть ли в Салавате джаз на следующей неделе?", now=NOW)
    assert [e.title for e in result.events] == ["Джазовый вечер"] and "не нашлось" not in result.reply


def test_the_blocking_condition_is_named(session, seeded):
    result = agent.ask(session, "в воскресенье до 100 рублей", now=NOW)
    assert "Ослабил условие по цене: именно оно мешало" in result.reply


def test_city_typo_is_fixed_and_the_user_is_told(session, add):
    add("Концерт в Стерлитамаке", datetime(2026, 11, 8, 19, 0), city="Стерлитамак", category="concert", price_min=500)
    add("Концерт в Уфе", datetime(2026, 11, 8, 19, 0), city="Уфа", category="concert", price_min=500)
    result = agent.ask(session, "Концерт в стерлитакмае в воскресенье", now=NOW)
    assert [e.title for e in result.events] == ["Концерт в Стерлитамаке"]
    assert "Поправил опечатку: «стерлитакмае» → Стерлитамак" in result.reply


def test_unchecked_conditions_are_never_dropped_silently(session, add):
    add("Спектакль «Макбет»", datetime(2026, 11, 8, 12, 0), city="Стерлитамак", category="theatre", price_min=500)
    result = agent.ask(
        session, "В воскресенье в Стерлитамаке, закончилось не позже 20:00 и ехать не больше 20 минут", now=NOW)
    assert result.events and "Не смог учесть" in result.reply
    assert "не позже 20:00" in result.reply and "ехать не больше 20 минут" in result.reply


def test_language_is_a_real_filter_and_its_sparse_data_is_explained(session, seeded):
    result = agent.ask(session, "детям в воскресенье на башкирском языке", now=NOW)
    assert "Ослабил условие по языку: именно оно мешало" in result.reply  # язык у этих событий не указан
    assert "где данные указаны" in result.reply and result.events


def test_language_filter_finds_events_where_the_language_is_known(session, add):
    add("Спектакль для малышей", datetime(2026, 11, 8, 12, 0), category="kids", description="Спектакль на башкирском языке.")
    add("Обычный спектакль", datetime(2026, 11, 8, 14, 0), category="kids")
    result = agent.ask(session, "детям в воскресенье на башкирском языке", now=NOW)
    assert [e.title for e in result.events] == ["Спектакль для малышей"] and "Ослабил" not in result.reply


def test_group_budget_is_applied_per_ticket(session, add):
    add("Недорогой спектакль", datetime(2026, 11, 8, 12, 0), category="theatre", price_min=400, price_max=450)
    add("Дорогой спектакль", datetime(2026, 11, 8, 18, 0), category="theatre", price_min=900, price_max=1500)
    result = agent.ask(session, "спектакли в воскресенье до 1000 рублей на двоих", now=NOW)
    assert [e.title for e in result.events] == ["Недорогой спектакль"]  # 500 ₽ за билет
    assert "500 ₽ за билет (1000 ₽ на двоих)" in result.reply


def test_subscription_requests_are_pointed_to_the_bot_and_change_nothing(session, seeded):
    from app.models import Subscription

    result = agent.ask(session, "Перенеси мою подписку на другой город", now=NOW)
    assert "/subscribe" in result.reply and "/unsubscribe" in result.reply and not result.events
    assert session.query(Subscription).count() == 0
