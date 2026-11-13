from datetime import datetime
from types import SimpleNamespace

from app import bot
from app.formatting import price_text, render_event, when_text
from app.seed import seed


def ev(**kw):
    defaults = dict(title="Тест", start_at=datetime(2026, 11, 8, 18, 30), end_at=None, series_until=None, price_min=None, price_max=None,
                    age_limit=0, status="scheduled", venue="", city="Уфа", address="", description="", sources=[])
    return SimpleNamespace(**{**defaults, **kw})


def test_price_text_variants():
    assert price_text(ev()) == "Цена не указана"
    assert price_text(ev(price_min=0, price_max=0)) == "Бесплатно"
    assert price_text(ev(price_min=0)) == "Бесплатно"
    assert price_text(ev(price_min=300)) == "от 300 ₽"
    assert price_text(ev(price_min=300, price_max=300)) == "300 ₽"
    assert price_text(ev(price_min=300, price_max=900)) == "300–900 ₽"
    assert price_text(ev(price_max=500)) == "до 500 ₽"


def test_when_text_for_single_and_multi_day_events():
    assert when_text(ev()) == "вс, 8 ноября, 18:30"
    assert when_text(ev(start_at=datetime(2026, 11, 8))) == "вс, 8 ноября"
    exhibition = ev(start_at=datetime(2026, 11, 1), end_at=datetime(2026, 11, 20, 20))
    assert when_text(exhibition, now=datetime(2026, 10, 25)) == "1 ноября – 20 ноября"  # ещё не началась
    assert when_text(exhibition, now=datetime(2026, 11, 7)) == "до 20 ноября"  # уже идёт


def test_render_event_escapes_html_for_telegram():
    text = render_event(ev(title="<b>Опасно</b> & «кавычки»", status="cancelled"), html=True)
    assert "<b>&lt;b&gt;Опасно&lt;/b&gt; &amp; «кавычки»</b>" in text
    assert "Отменено" in text


def test_long_description_is_trimmed_before_escaping_and_clip_never_splits_html():
    long = ev(description="Q & A " * 1000)
    text = render_event(long, html=True, details=True)
    assert len(text) < bot.MAX_TEXT and text.rstrip().endswith("…") and "&amp;" in text
    assert "&am" not in text.replace("&amp;", "")  # ни одной обрезанной сущности

    cut = bot.clip("<b>заголовок</b>\n" + "&amp;" * 2000)  # одна огромная строка без переводов строки
    assert len(cut) <= bot.MAX_TEXT and not cut.endswith(("&", "&a", "&am", "&amp"))
    lines = bot.clip("<b>строка</b>\n" * 400).split("\n")
    assert all(line == "<b>строка</b>" for line in lines)  # режется по границе строки, теги целы


def test_quick_filters_and_menu():
    assert bot.quick_filters("free", None).free_only
    assert bot.quick_filters("kids", "Уфа").kids and bot.quick_filters("kids", "Уфа").city == "Уфа"
    assert bot.quick_filters("cat_theatre", None).category == "theatre"
    weekend = bot.quick_filters("weekend", None)
    assert weekend.date_from <= weekend.date_to
    try:
        bot.quick_filters("nonsense", None)
    except ValueError:
        pass
    else:
        raise AssertionError("ожидали ValueError")
    buttons = [b for row in bot.menu_markup("Уфа").inline_keyboard for b in row]
    assert len(buttons) == len(bot.QUICK) + 2


def test_menu_gets_open_app_button_only_when_webapp_url_is_set(monkeypatch):
    monkeypatch.setattr(bot.config, "WEBAPP_URL", "")
    assert not any(b.web_app for row in bot.menu_markup().inline_keyboard for b in row)
    monkeypatch.setattr(bot.config, "WEBAPP_URL", "https://afisha.example.org")
    first = bot.menu_markup().inline_keyboard[0][0]
    assert first.web_app.url == "https://afisha.example.org" and "афишу" in first.text


def test_list_view_paginates_and_card_view_shows_sources(session):
    seed(session)
    view = bot.list_view(session, "week", None)
    callbacks = [b.callback_data for row in view.markup.inline_keyboard for b in row]
    assert f"l:week:{bot.PAGE}" in callbacks  # есть кнопка «Ещё»
    assert sum(c.startswith("e:") for c in callbacks) == bot.PAGE
    assert len(view.text) <= bot.MAX_TEXT

    event_id = int(next(c for c in callbacks if c.startswith("e:")).split(":")[1])
    card = bot.event_view(session, event_id)
    assert card is not None and "Источники:" in card.text
    assert bot.event_view(session, 999999) is None


def test_callback_data_fits_telegram_limit(session):
    seed(session)
    markups = [bot.menu_markup("Стерлитамак"), bot.categories_markup(session), bot.cities_markup(session)[0],
               bot.list_view(session, "free", "Уфа").markup]
    for markup in markups:
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data:
                    assert len(button.callback_data.encode()) <= 64


def test_free_text_goes_through_the_agent_in_rules_mode(session, monkeypatch):
    seed(session)
    monkeypatch.setattr(bot, "SessionLocal", lambda: _Ctx(session))
    bot.chat_city[42] = "Уфа"
    view = bot.agent_view(42, "бесплатно")
    assert "Нашёл" in view.text
    assert bot.chat_history[42][-1]["role"] == "assistant"


class _Ctx:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *exc):
        return False
