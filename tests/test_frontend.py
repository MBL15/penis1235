"""Фронтенд на Tailwind: собранный CSS на месте, подключён, и в нём есть то, что не видно по разметке."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
KEYFRAMES = ("settle", "rise", "draw", "pop", "appear", "bounce", "sweep", "drift-a", "drift-b", "fade", "bubble-in", "grow")


def css() -> str:
    return (STATIC / "tailwind.css").read_text(encoding="utf-8")


def test_built_stylesheet_is_served_and_linked(client):
    assert 'href="/static/tailwind.css"' in (STATIC / "index.html").read_text(encoding="utf-8")
    response = client.get("/static/tailwind.css")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/css")
    assert not (STATIC / "style.css").exists()  # старый ручной CSS не должен вернуться рядом с Tailwind


def test_every_keyframe_is_emitted():
    """Tailwind добавляет ключевые кадры, только если на них ссылается класс animate-*; остальные (reveal, прогресс
    прокрутки) держатся на плагине. Если кадр пропал, анимация молча перестаёт работать."""
    missing = [name for name in KEYFRAMES if f"@keyframes {name}{{" not in css()]
    assert not missing, f"нет ключевых кадров: {missing}"


def test_states_set_by_javascript_have_styles():
    text = css()
    for needle in (
        ".tg .tg\\:hidden",  # режим мини-приложения Telegram
        ".js .reveal.in",  # появление при прокрутке
        ".js .reveal:not(.in)",
        ".scroll-progress:before",  # полоса прогресса
        ".spot:hover:before",  # подсветка под курсором
        ".tone-theatre",  # цвета категорий (классы собираются в app.js из слага)
        ".tone-other",
        "prefers-reduced-motion:reduce",
        "[hidden]",
    ):
        assert needle in text, needle


def test_markup_has_no_double_quotes_inside_class_attributes():
    """Двойная кавычка внутри класса (например after:content-["x"]) обрывает атрибут class в готовой разметке."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    broken = re.findall(r'class="[^"$`]*(?:\$\{[^}]*\}[^"$`]*)*\[\\?"', js)
    assert not broken, broken[:3]
    assert 'content-["' not in js and "content-[\\'" not in js


def test_no_inline_styles_for_layout():
    """Раскладка и цвета задаются утилитами. Допустимы только переменные --i (порядок появления) и задержки анимации."""
    text = (STATIC / "index.html").read_text(encoding="utf-8")
    styles = re.findall(r'style="([^"]*)"', text)
    assert all(re.fullmatch(r"--i:\d+", s) for s in styles), styles
