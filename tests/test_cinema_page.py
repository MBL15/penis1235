"""Страница «Кино» (/cinema): фильмы дня, кинотеатры, время сеансов, день и город в адресе, пустые состояния."""
from datetime import timedelta

from app.cinema_page import _split_description, group_films
from app.config import now_local

DESC = ("Драма, Спортивный. Россия, 2026. Длительность: 2 часа 24 минуты. Это история о людях, которые решили сделать невозможное. "
        "Сеансы 14.11: 12:00 (2D), 14:30 (2D), 21:15.")


def day(offset=1):
    return (now_local() + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)


def film(add, title, hour, venue, city="Уфа", **kw):
    event, _ = add(title, day().replace(hour=hour), category="cinema", venue=venue, city=city, description=kw.pop("description", DESC),
                   price_min=kw.pop("price_min", 300), age_limit=kw.pop("age_limit", 12), **kw)
    return event


def test_description_is_split_into_facts_synopsis_and_session_times():
    facts, synopsis, times = _split_description(DESC)
    assert facts == "Драма, Спортивный. Россия, 2026. Длительность: 2 часа 24 минуты."
    assert synopsis.startswith("Это история") and times == ["12:00 (2D)", "14:30 (2D)", "21:15"]
    # чужой текст без нашего формата не ломает разбор: показывается как есть, времени нет
    assert _split_description("Просто описание") == ("", "Просто описание", [])
    assert _split_description(None) == ("", "", [])


def test_same_film_in_two_cinemas_is_one_tile_with_both_cinemas(session, add):
    film(add, "Битва моторов", 12, "Кинопростор")
    film(add, "Битва моторов", 18, "Ultra Cinema", description="Сеансы 14.11: 18:00.", price_min=250)
    film(add, "Другой фильм", 15, "Кинопростор")
    session.flush()
    from app.search import SearchFilters, search
    events, _ = search(session, SearchFilters(category="cinema", date_from=day().date(), date_to=day().date(), limit=50))
    films = group_films(events)
    assert [f["title"] for f in films] == ["Битва моторов", "Другой фильм"]
    battle = films[0]
    assert [p["venue"] for p in battle["places"]] == ["Кинопростор", "Ultra Cinema"] and min(battle["prices"]) == 250
    assert battle["places"][1]["times"] == ["18:00"] and battle["facts"].startswith("Драма")


def test_cinema_page_shows_films_times_and_links(client, add):
    event = film(add, "Битва моторов", 12, "Кинопростор")
    page = client.get("/cinema", params={"day": day().date().isoformat()})
    assert page.status_code == 200 and "Кино" in page.text
    assert "Битва моторов" in page.text and "Кинопростор" in page.text and "12:00 (2D)" in page.text and "21:15" in page.text
    assert f'href="/event/{event.id}"' in page.text and "12+" in page.text and "от 300 ₽" in page.text
    assert "Длительность: 2 часа 24 минуты" in page.text and "Афиши (afisha.ru)" in page.text


def test_cinema_page_filters_by_city_and_day_from_the_url(client, add):
    film(add, "Уфимский фильм", 12, "Кинопростор", city="Уфа")
    film(add, "Стерлитамакский фильм", 13, "Мир кино", city="Стерлитамак")
    d = day().date().isoformat()
    both = client.get("/cinema", params={"day": d}).text
    assert "Уфимский фильм" in both and "Стерлитамакский фильм" in both and "Все города" in both
    ufa = client.get("/cinema", params={"day": d, "city": "Уфа"}).text
    assert "Уфимский фильм" in ufa and "Стерлитамакский фильм" not in ufa
    # неизвестный город и неверный день не ломают страницу: показывается сегодняшний день, все города
    assert client.get("/cinema", params={"city": "Атлантида", "day": "не дата"}).status_code == 200
    assert client.get("/cinema", params={"day": (day(30)).date().isoformat()}).status_code == 200  # дальше окна: сегодня


def test_cinema_page_empty_states_are_honest(client, add):
    assert "пока не загружено" in client.get("/cinema").text  # кино в базе нет совсем
    film(add, "Завтрашний фильм", 12, "Кинопростор")
    today_page = client.get("/cinema").text
    assert "сеансов не нашлось" in today_page and "другой день" in today_page  # на сегодня пусто, но есть завтра


def test_cinema_page_escapes_titles_and_venues(client, add):
    film(add, "<script>alert(1)</script>", 12, "Кино <b>")
    text = client.get("/cinema", params={"day": day().date().isoformat()}).text
    assert "<script>alert(1)" not in text and "&lt;script&gt;" in text and "Кино <b>" not in text


def test_cinema_tab_is_not_in_the_header_but_page_still_opens(client):
    # вкладку «Кино» убрали из верхнего меню (14.11.2026); страница живёт по прямой ссылке
    assert 'href="/cinema"' not in client.get("/plan").text
    assert 'href="/cinema"' not in client.get("/").text
    assert client.get("/cinema").status_code == 200
