"""Пометка «неофициальный сервис» (ТЗ §13) и объявленный уровень решения (ТЗ §1, §5) не должны пропасть."""
from pathlib import Path

from app import bot, config

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"


def test_site_says_it_is_not_an_official_service():
    text = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="disclaimer"' in text and "Неофициальный сервис" in text and "Министерства культуры" in text
    assert "из официальных источников" not in text  # большая часть данных идёт из коммерческих афиш


def test_widget_always_shows_the_note():
    text = (STATIC / "widget.js").read_text(encoding="utf-8")
    assert "Неофициальный сервис" in text and "data-disclaimer" in text


def test_bot_welcome_carries_the_note():
    assert "Неофициальный сервис" in bot.WELCOME


def test_api_exposes_the_note(client):
    assert client.get("/api/meta").json()["disclaimer"] == config.DISCLAIMER
    assert config.DISCLAIMER in client.get("/openapi.json").json()["info"]["description"]


def test_readme_declares_the_level():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Выбранный уровень решения" in readme and "**Уровень 2" in readme
