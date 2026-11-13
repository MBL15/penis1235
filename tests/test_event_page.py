"""Карточка события с постоянной ссылкой (ТЗ §6.3): /event/{id}."""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import bot, config
from app.config import now_local
from app.pipeline import process_record

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
START = (now_local() + timedelta(days=5)).replace(hour=19, minute=0, second=0, microsecond=0)
LD_JSON = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


@pytest.fixture
def event(add, session):
    event, _ = add("Концерт симфонического оркестра", START, venue="Филармония", address="ул. Ленина, 5", city="Уфа",
                   category="concert", price_min=300, price_max=1200, age_limit=6, url="https://tickets.example/1",
                   description="Первый абзац программы.\n\nВторой абзац с подробностями.", source="philharmonia.example",
                   source_url="https://philharmonia.example/e/1", image_url="https://img.example/1.jpg")
    return event


def page(client, event_id):
    return client.get(f"/event/{event_id}")


def test_card_shows_everything_a_resident_needs(client, event):
    response = page(client, event.id)
    html = response.text
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    assert "<title>Концерт симфонического оркестра — Афиша культуры Башкортостана</title>" in html
    assert "<h1" in html and "Филармония, Уфа" in html and "ул. Ленина, 5" in html
    assert "300–1200 ₽" in html and "6+" in html and "Концерт" in html
    assert "Первый абзац программы." in html and "Второй абзац с подробностями." in html
    assert 'href="https://tickets.example/1"' in html and 'rel="noopener noreferrer"' in html
    assert "Откуда информация" in html and "philharmonia.example" in html and 'href="https://philharmonia.example/e/1"' in html
    assert f'href="/feedback?event={event.id}"' in html and f'href="/event/{event.id}"' in html
    assert config.DISCLAIMER in html  # пометка «неофициальный сервис» есть и на карточке
    assert 'lang="ru"' in html and '<meta name="viewport"' in html


def test_page_carries_preview_metadata_and_structured_data(client, event):
    html = page(client, event.id).text
    assert f'<link rel="canonical" href="{config.PUBLIC_URL}/event/{event.id}">' in html
    assert 'property="og:title" content="Концерт симфонического оркестра — Афиша культуры Башкортостана"' in html
    assert 'property="og:image" content="https://img.example/1.jpg"' in html
    data = json.loads(LD_JSON.search(html).group(1))
    assert data["@type"] == "Event" and data["name"] == "Концерт симфонического оркестра" and data["eventStatus"].endswith("EventScheduled")
    assert data["location"]["name"] == "Филармония"


def test_page_has_no_executable_inline_scripts(client, event):
    response = page(client, event.id)
    inline = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", response.text)
    assert all('type="application/ld+json"' in attrs for attrs in inline)  # данные для поисковиков, не код
    assert not re.search(r"\son[a-z]+\s*=", response.text)
    csp = response.headers["content-security-policy"]
    assert "script-src 'self' https://telegram.org" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]


def test_values_from_sources_are_escaped(client, add):
    evil, _ = add("<script>alert(1)</script> & «кавычки»", START, description="<img src=x onerror=alert(1)>", venue='"><b>x</b>',
                  source="</script><script>alert(2)</script>")
    html = page(client, evil.id).text
    assert "<script>alert" not in html and "<img src=x" not in html and "<b>x</b>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; «кавычки»" in html
    ld = LD_JSON.search(html).group(1)
    assert "</" not in ld and json.loads(ld)["name"].startswith("<script>")  # в JSON-LD закрывающий тег экранирован


def test_unsafe_links_are_never_rendered(client, event, session):
    event.url = "javascript:alert(1)"
    event.sources = [{"name": "bad", "url": "javascript:alert(2)", "status": "scheduled"}]
    session.commit()
    html = page(client, event.id).text
    assert "javascript:" not in html and "Источник и билеты" not in html


def test_cancelled_event_is_reachable_by_its_link_with_a_clear_mark(client, add):
    event, _ = add("Спектакль «Вишнёвый сад»", START, status="cancelled", url="https://tickets.example/2")
    assert client.get("/api/v1/events", params={"q": "вишневый"}).json()["total"] == 0  # из выдачи убран
    response = page(client, event.id)
    html = response.text
    assert response.status_code == 200
    assert "<title>ОТМЕНЕНО: Спектакль «Вишнёвый сад»" in html
    assert 'role="alert"' in html and "Событие отменено." in html and "line-through" in html
    assert json.loads(LD_JSON.search(html).group(1))["eventStatus"].endswith("EventCancelled")


@pytest.mark.parametrize("status, text", [("rescheduled", "Событие перенесено."), ("sold_out", "Билеты проданы.")])
def test_other_statuses_get_their_own_notice(client, add, status, text):
    event, _ = add(f"Событие {status}", START, status=status)
    assert text in page(client, event.id).text


def test_series_and_missing_data_are_stated_honestly(client, add, session):
    series, _ = add("Кровь хищника", START, series_until=START + timedelta(days=21))
    html = page(client, series.id).text
    assert "показы до" in html and "Серия показов: известен один показ" in html
    assert "В источнике нет описания." in html and "Цена не указана" in html
    assert "статус источником не указан" not in html  # у источника статус указан
    series.sources = [{"name": "culture.ru", "url": "https://culture.ru/x"}]
    session.commit()
    assert "статус источником не указан" in page(client, series.id).text


def test_several_sources_are_listed_and_counted(client, add):
    first, _ = add("Балет «Лебединое озеро»", START, source="a.example", source_url="https://a.example/1")
    add("Лебединое озеро. Балет П. И. Чайковского", START, source="b.example", source_url="https://b.example/1")
    html = page(client, first.id).text
    assert "подтверждено 2 источниками" in html and "a.example" in html and "b.example" in html


def test_a_past_event_says_so(client, add):
    old, _ = add("Давний концерт", datetime(2026, 10, 1, 19, 0))
    assert "Это событие уже прошло." in page(client, old.id).text


def test_drafts_and_bad_numbers_give_a_friendly_404(client, session):
    draft = process_record(session, {"title": "Черновик", "start_at": START.isoformat(), "source": "x"}, publish=False).event
    for target in (draft.id, 999999, "abc", "0", "9" * 11, "2147483648"):
        response = client.get(f"/event/{target}")
        assert response.status_code == 404 and "Такого события нет" in response.text and 'content="noindex"' in response.text
        assert response.headers["content-type"].startswith("text/html")


def test_page_is_cached_with_etag(client, event, session):
    first = page(client, event.id)
    assert first.headers["cache-control"] == "public, max-age=60"
    assert client.get(f"/event/{event.id}", headers={"If-None-Match": first.headers["etag"]}).status_code == 304
    event.title = "Новое название"
    session.commit()
    changed = client.get(f"/event/{event.id}", headers={"If-None-Match": first.headers["etag"]})
    assert changed.status_code == 200 and "Новое название" in changed.text


def test_site_cards_and_widget_link_to_the_card_page():
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'href="/event/${Number(event.id) || 0}"' in script
    assert "откроется в новой вкладке" not in script.split("function cardHtml")[1].split("// Заглушка")[0]
    assert "'/event/'" in (STATIC / "widget.js").read_text(encoding="utf-8")


def test_bot_card_offers_the_permanent_link_only_over_https(monkeypatch, session, event):
    monkeypatch.setattr(config, "PUBLIC_URL", "http://localhost:8000")
    texts = [b.text for row in bot.event_view(session, event.id).markup.inline_keyboard for b in row]
    assert "🔗 Источник и билеты" in texts and not any("Карточка" in t for t in texts)
    monkeypatch.setattr(config, "PUBLIC_URL", "https://afisha.example")
    buttons = [b for row in bot.event_view(session, event.id).markup.inline_keyboard for b in row]
    card = next(b for b in buttons if "Карточка" in b.text)
    assert card.url == f"https://afisha.example/event/{event.id}"
