"""Разбор запроса правилами: дефекты, найденные первым прогоном контрольных запросов (eval/), и их исправления."""
from datetime import date, datetime, timedelta

from app import agent
from app.rules import month_only, month_range, parse_conversation, parse_query, residual_words

from .conftest import NOW

SATURDAY = date(2026, 11, 7)


def test_family_phrases_mean_children_but_seven_is_just_a_number():
    for text in ("в воскресенье с детьми", "театр для всей семьи", "с семьёй на выходных", "куда с детишками"):
        assert parse_query(text, SATURDAY).filters.kids is True, text
    assert parse_query("концерт в семь вечера", SATURDAY).filters.kids is None


def test_month_without_a_day_is_the_whole_month():
    assert month_only(["что", "в", "декабре"]) == 12
    assert month_only(["9", "мая"]) is None  # с числом это точная дата, другая ветка разбора
    f = parse_query("что в декабре", SATURDAY).filters
    assert (f.date_from, f.date_to) == (date(2026, 12, 1), date(2026, 12, 31))
    f = parse_query("концерты в январе", SATURDAY).filters
    assert (f.date_from, f.date_to) == (date(2027, 1, 1), date(2027, 1, 31))  # январь этого года уже прошёл
    assert month_range(11, SATURDAY) == (SATURDAY, date(2026, 11, 30))  # текущий месяц — с сегодняшнего дня
    f = parse_query("9 мая", SATURDAY).filters
    assert f.date_from == f.date_to == date(2027, 5, 9)


def test_typo_in_a_category_is_corrected_and_reported():
    parsed = parse_query("спектакь в Уфе", SATURDAY)
    assert parsed.filters.category == "theatre" and parsed.filters.q is None
    assert "«спектакь» → спектакль" in parsed.corrections


def test_unknown_words_are_searched_not_dropped():
    parsed = parse_query("концерт Земфиры в Стерлитамаке", SATURDAY)
    assert parsed.filters.q == "земфиры" and parsed.guessed_q and parsed.filters.category == "concert"
    parsed = parse_query("цирк в Уфе", SATURDAY)
    assert parsed.filters.q == "цирк" and parsed.intent  # раньше такой вопрос получал просьбу уточнить
    # слова просьб, компании и опечатки в известных словах за искомое не принимаются
    assert residual_words("Куда пойти в субботу с ребёнком до 1000 рублей?") == []
    assert residual_words("посоветуй что-нибудь интересное на вечер с девушкой") == []
    assert parse_query("Концерт в стерлитакмае в субботу", SATURDAY).filters.q is None


def test_unknown_word_alone_does_not_replace_the_clarifying_question():
    parsed = parse_query("что-нибудь интересное", SATURDAY)
    assert parsed.filters.q is None and not parsed.intent
    assert not parse_query("хочу развлечься", SATURDAY).intent or parse_query("хочу развлечься", SATURDAY).filters.q is None


def test_cheaper_with_a_sum_keeps_what_was_shown_but_bare_cheaper_asks_for_others():
    with_sum = parse_conversation(["концерты в Уфе в субботу", "а подешевле, до 1500"], SATURDAY, [1, 2])
    assert with_sum.filters.price_max == 1500 and with_sum.filters.exclude_ids == []
    bare = parse_conversation(["концерты в Уфе в субботу", "а подешевле"], SATURDAY, [1, 2])
    assert bare.filters.exclude_ids == [1, 2] and bare.filters.sort == "price"


# ---------- ответы агента ----------

def test_word_that_matches_nothing_is_said_so_and_dropped_first(session, add):
    add("Концерт «Вечер романсов»", NOW + timedelta(days=1, hours=3), city="Уфа", category="concert", price_min=500)
    result = agent.ask(session, "концерт Земфиры в Уфе", now=NOW)
    assert "со словом «земфиры» не нашлось" in result.reply and "Без этого слова нашёл 1 событие" in result.reply
    assert [e.title for e in result.events] == ["Концерт «Вечер романсов»"]


def test_guessed_word_is_dropped_before_the_date(session, add):
    add("Концерт «Вечер романсов»", datetime(2026, 11, 8, 19, 0), city="Уфа", category="concert", price_min=500)
    result = agent.ask(session, "что в Уфе в воскресенье с девушкой Земфирой", now=NOW)
    assert "дате" not in result.reply and "со словом" in result.reply  # мешало слово, а не воскресенье
    assert len(result.events) == 1


def test_cancelled_event_is_mentioned_but_never_listed_as_current(session, add):
    add("Гала-концерт «Осенний марафон»", datetime(2026, 11, 8, 19, 30), city="Уфа", category="concert", status="cancelled")
    add("Концерт «Вечер романсов»", datetime(2026, 11, 8, 18, 0), city="Уфа", category="concert", price_min=500)
    result = agent.ask(session, "концерты в Уфе в воскресенье", now=NOW)
    assert [e.title for e in result.events] == ["Концерт «Вечер романсов»"]
    assert "Отменено: «Гала-концерт «Осенний марафон»»" in result.reply
    asked = agent.ask(session, "Состоится ли концерт «Осенний марафон»?", now=NOW)
    assert "Отменено" in asked.reply and all(e.status != "cancelled" for e in asked.events)


def test_nothing_cancelled_means_no_note(session, add):
    add("Концерт «Вечер романсов»", datetime(2026, 11, 8, 18, 0), city="Уфа", category="concert", price_min=500)
    assert "Отменено" not in agent.ask(session, "концерты в Уфе", now=NOW).reply


def test_time_duration_and_period_words_are_not_unknown_words():
    for text in ("с ребёнком 7 лет в субботу, не дольше двух часов и недорого", "бесплатные выставки в Уфе до конца месяца",
                 "ребёнку 8 лет, ехать не больше 20 минут, бюджет 1000 рублей на двоих"):
        assert parse_query(text, SATURDAY).filters.q is None, text


def test_question_about_cancellation_gets_a_status_answer(session, add):
    add("Спектакль «Ревизор»", datetime(2026, 11, 8, 18, 0), city="Уфа", category="theatre", price_min=500)
    reply = agent.ask(session, "Отменили ли спектакль в Уфе 8 ноября?", now=NOW).reply
    assert "Отменённых событий по этому запросу в афише нет" in reply and "не всегда" in reply  # не «молчание = не отменили»
    add("Спектакль «Мастер и Маргарита»", datetime(2026, 11, 8, 19, 0), city="Уфа", category="theatre", status="cancelled")
    reply = agent.ask(session, "Отменили ли спектакль в Уфе 8 ноября?", now=NOW).reply
    assert "Отменено: «Спектакль «Мастер и Маргарита»»" in reply and "Отменённых событий" not in reply
    assert "Отменённых событий" not in agent.ask(session, "спектакли в Уфе 8 ноября", now=NOW).reply  # не спрашивали про отмену
