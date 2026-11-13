from datetime import date

from app.rules import parse_conversation, parse_query

SATURDAY = date(2026, 11, 7)
WEDNESDAY = date(2026, 11, 4)


def test_headline_example_from_the_brief():
    parsed = parse_query("Куда пойти в субботу с ребёнком до 1000 рублей?", SATURDAY)
    f = parsed.filters
    assert parsed.recognized
    assert f.date_from == f.date_to == SATURDAY  # сегодня суббота, значит «в субботу» — это сегодня
    assert f.kids is True
    assert f.price_max == 1000
    assert f.free_only is False


def test_weekday_resolves_to_next_occurrence():
    f = parse_query("что в пятницу", WEDNESDAY).filters
    assert f.date_from == date(2026, 11, 6)
    f = parse_query("что в среду", WEDNESDAY).filters
    assert f.date_from == WEDNESDAY


def test_relative_days_and_weekend():
    assert parse_query("завтра", SATURDAY).filters.date_from == date(2026, 11, 8)
    assert parse_query("послезавтра", SATURDAY).filters.date_from == date(2026, 11, 9)
    f = parse_query("чем заняться на выходных", WEDNESDAY).filters
    assert (f.date_from, f.date_to) == (date(2026, 11, 7), date(2026, 11, 8))
    f = parse_query("что на следующей неделе", SATURDAY).filters
    assert (f.date_from, f.date_to) == (date(2026, 11, 9), date(2026, 11, 15))


def test_explicit_date_rolls_over_to_next_year():
    assert parse_query("15 ноября", SATURDAY).filters.date_from == date(2026, 11, 15)
    assert parse_query("3 марта", SATURDAY).filters.date_from == date(2027, 3, 3)
    assert parse_query("9 мая", SATURDAY).filters.date_from == date(2027, 5, 9)


def test_price_variants():
    assert parse_query("до 1 500 рублей", SATURDAY).filters.price_max == 1500
    assert parse_query("не дороже 700", SATURDAY).filters.price_max == 700
    assert parse_query("до 2 тысяч", SATURDAY).filters.price_max == 2000
    free = parse_query("бесплатные выставки", SATURDAY).filters
    assert free.free_only and free.category == "exhibition"


def test_child_age_is_not_mistaken_for_price():
    f = parse_query("с ребёнком до 5 лет", SATURDAY).filters
    assert f.kids is True and f.age == 5
    assert f.price_max is None


def test_city_and_category():
    f = parse_query("концерты в Стерлитамаке", SATURDAY).filters
    assert f.city == "Стерлитамак" and f.category == "concert"
    f = parse_query("спектакль в уфе", SATURDAY).filters
    assert f.city == "Уфа" and f.category == "theatre"


def test_vague_question_is_not_recognized():
    parsed = parse_query("что посоветуешь?", SATURDAY)
    assert not parsed.recognized


def test_clarification_answer_extends_the_original_question():
    parsed = parse_conversation(["с ребёнком до 1000 рублей", "в субботу"], SATURDAY)
    f = parsed.filters
    assert f.kids and f.price_max == 1000 and f.date_from == SATURDAY
    # новая цена заменяет прежнюю, «бесплатно» отменяет лимит
    f = parse_conversation(["до 500 рублей", "лучше бесплатно"], SATURDAY).filters
    assert f.free_only and f.price_max is None
