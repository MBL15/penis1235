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


# ---------- ТЗ, Приложение А: ничего не теряем молча ----------

def test_specific_words_become_search_words_not_just_a_category():
    f = parse_query("Есть ли в Салавате джаз на следующей неделе?", SATURDAY).filters
    assert f.q == "джаз" and f.city == "Салават" and f.category is None
    f = parse_query("Сколько стоит билет на ближайший балет?", SATURDAY).filters
    assert f.q == "балет" and f.category is None
    f = parse_query("концерт джаза", SATURDAY).filters  # общее слово «концерт» остаётся категорией
    assert f.category == "concert" and f.q == "джаза"
    assert parse_query("концерты в Уфе", SATURDAY).filters.q is None


def test_city_typos_are_fixed_and_reported_but_months_are_not_cities():
    parsed = parse_query("Концерт в стерлитакмае в субботу", SATURDAY)
    assert parsed.filters.city == "Стерлитамак" and parsed.corrections == ["«стерлитакмае» → Стерлитамак"]
    assert parse_query("концерты в октябре", SATURDAY).filters.city is None
    assert parse_query("что посмотреть сегодня", SATURDAY).filters.city is None
    assert parse_query("спектакль в Уфе", SATURDAY).corrections == []  # точное совпадение не считается опечаткой


def test_conditions_we_cannot_check_are_listed():
    text = "Куда пойти в субботу с ребёнком 8 лет в Стерлитамаке, до 1000 рублей на двоих, чтобы закончилось не позже 20:00 и ехать не больше 20 минут?"
    parsed = parse_query(text, SATURDAY)
    assert parsed.unsupported == ["время («не позже 20:00»)", "расстояние и дорога («ехать не больше 20 минут»)"]
    assert parsed.filters.city == "Стерлитамак" and parsed.filters.age == 8 and parsed.filters.kids


def test_other_unsupported_conditions():
    assert parse_query("что посмотреть сегодня вечером", SATURDAY).unsupported == ["время («вечером»)"]
    assert parse_query("концерт в пятницу после 19:00", SATURDAY).unsupported == ["время («после 19:00»)"]
    assert parse_query("с ребёнком, не дольше двух часов", SATURDAY).unsupported == ["длительность («не дольше двух часов»)"]
    assert parse_query("мероприятия на башкирском языке", SATURDAY).unsupported == ["язык мероприятия («на башкирском языке»)"]
    assert parse_query("а есть что-то ближе к центру?", SATURDAY).unsupported == ["расстояние и дорога («ближе к центру»)"]
    assert parse_query("концерты в Уфе в субботу до 1500 рублей", SATURDAY).unsupported == []  # обычные условия не трогаем


def test_clock_time_is_not_a_price():
    f = parse_query("спектакль до 20:00", SATURDAY).filters
    assert f.price_max is None


def test_budget_for_a_group_is_split_per_ticket():
    parsed = parse_query("до 1000 рублей на двоих", SATURDAY)
    assert parsed.filters.price_max == 500 and "до 500 ₽ за билет (1000 ₽ на двоих)" in parsed.notes
    assert parse_query("2000 рублей на двоих", SATURDAY).filters.price_max == 1000
    assert parse_query("до 900 рублей на троих", SATURDAY).filters.price_max == 300
    assert parse_conversation(["до 1000 рублей на двоих", "в субботу"], SATURDAY).filters.price_max == 500


def test_end_of_month_and_cheap():
    f = parse_query("бесплатные выставки до конца месяца", date(2026, 11, 13)).filters
    assert (f.date_from, f.date_to) == (date(2026, 11, 13), date(2026, 11, 30)) and f.free_only
    assert parse_query("что-нибудь недорого в субботу", SATURDAY).filters.sort == "price"
    assert parse_conversation(["в субботу", "недорого"], SATURDAY).filters.sort == "price"
