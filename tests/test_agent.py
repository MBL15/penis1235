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
    # раньше отвечало «Нашёл 1 событие» про не-джаз, потом показывало его с оговоркой; теперь честный отказ (ревью Т-9)
    assert "событий со словом «джаз» в афише нет" in result.reply and "1 событие" in result.reply
    assert result.events == []


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


# ---------- карточки по названиям, если модель не вернула event_ids ----------

def _final(reply, ids=None, **extra):
    import json

    return json.dumps({"reply": reply, "event_ids": ids if ids is not None else [], "needs_clarification": False, **extra}, ensure_ascii=False)


@pytest.fixture
def named(add):
    events = [add(title, datetime(2026, 11, 8, 12 + number, 0), category="concert")[0]
              for number, title in enumerate(["Джазовый вечер", "Лекция", "Лекция о Дале", "Выставка «Золотая Орда»"])]
    return {e.id: e for e in events}, events


def test_cards_are_built_from_titles_named_in_the_reply_when_the_model_forgets_event_ids(named):
    seen, (jazz, lecture, lecture_dal, expo) = named
    result = agent._result_from_final(_final("Вот что нашлось: выставка «Золотая Орда» и Джазовый вечер, оба подходят."), seen, {})
    assert [e.id for e in result.events] == [expo.id, jazz.id] and result.ids_from_text  # в порядке упоминания


def test_plain_text_answer_without_json_still_gets_cards(named):
    seen, (jazz, *_rest) = named
    result = agent._result_from_final("В субботу есть Джазовый вечер, начало в 12:00.", seen, {})
    assert [e.id for e in result.events] == [jazz.id] and result.ids_from_text


def test_ids_from_the_model_are_never_overridden_by_titles(named):
    seen, (jazz, lecture, lecture_dal, expo) = named
    result = agent._result_from_final(_final("Советую Джазовый вечер и выставку «Золотая Орда».", [expo.id]), seen, {})
    assert [e.id for e in result.events] == [expo.id] and not result.ids_from_text


def test_no_cards_for_a_clarifying_question_or_for_a_reply_that_names_nothing(named):
    seen, (jazz, *_rest) = named
    assert not agent._result_from_final(_final("Уточните день. Например, про Джазовый вечер?", needs_clarification=True), seen, {}).events
    result = agent._result_from_final(_final("Подходящих событий не нашлось, попробуйте другой день."), seen, {})
    assert result.events == [] and not result.ids_from_text


def test_a_short_title_inside_a_longer_one_is_not_a_second_event_and_cancelled_events_are_not_recommended(named):
    seen, (jazz, lecture, lecture_dal, expo) = named
    result = agent._result_from_final(_final("Приходите на Лекция о Дале."), seen, {})
    assert [e.id for e in result.events] == [lecture_dal.id]  # «Лекция» не добавляется отдельной карточкой
    expo.status = "cancelled"
    result = agent._result_from_final(_final("Выставка «Золотая Орда» отменена."), seen, {})
    assert result.events == []


def test_only_events_returned_by_tools_can_become_cards(named, add):
    seen, _ = named
    stranger, _ = add("Балет «Жизель»", datetime(2026, 11, 9, 19, 0), category="theatre")  # в базе есть, поиск его не вернул
    result = agent._result_from_final(_final("Ещё есть Балет «Жизель»."), seen, {})
    assert result.events == []

def test_named_topic_missing_is_an_honest_refusal_without_other_cards(session, add):
    """ТЗ Прил. А «Есть ли в Салавате джаз на следующей неделе?»: джаза нет — нет и чужих карточек, только число и подсказка."""
    add("Спектакль «Ревизор»", datetime(2026, 11, 10, 18, 0), city="Салават", category="theatre")
    result = agent.ask(session, "джаз в Салавате на следующей неделе", now=NOW)
    assert result.events == [] and "джаз" in result.reply and "нет" in result.reply and "1 событие" in result.reply
