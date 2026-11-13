"""Базовая линия: поиск по ключевым словам, как у строки поиска без понимания условий (ТЗ §6.5).

Из вопроса берутся слова от трёх букв (без служебных), событие подходит, если в названии, описании, площадке, городе или
категории есть начало КАЖДОГО слова (первые пять букв). Даты, цены, возраст и время такой поиск не понимает: «в субботу до
1000 рублей» превращается в слова «суббот» и «1000», которых в карточках нет. Результат: до пяти ближайших событий."""
from datetime import datetime

from sqlalchemy.orm import Session

from app.normalize import CATEGORIES, norm_text
from app.search import SearchFilters, search

STOP = frozenset(
    "что где как для при про это эти или они она оно его ещё еще нет есть был была были будет может можно хочу хотим хотел "
    "хотела надо нужно мне нам вам при над под без из за от до по на не ни но да так там тут вот уже очень куда сходить пойти "
    "посоветуй покажи найди подскажи какие какой какая какое которые ближайшие что-нибудь чтонибудь".split()
)
STEM = 5


def keywords(text: str) -> list[str]:
    return [w[:STEM] for w in norm_text(text).split() if len(w) >= 3 and w not in STOP and not w.isdigit()]


def keyword_search(session: Session, text: str, now: datetime, limit: int = 5) -> list:
    words = keywords(text)
    if not words:
        return []
    events, _ = search(session, SearchFilters(limit=500), now)  # только опубликованные и не прошедшие: как на сайте
    found = []
    for event in events:
        hay = norm_text(f"{event.title} {event.description or ''} {event.venue or ''} {event.city} {CATEGORIES.get(event.category, '')}")
        if all(word in hay for word in words):
            found.append(event)
    return found[:limit]
