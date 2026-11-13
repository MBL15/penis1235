"""Дубликаты между источниками: склейка при приёме, сводка в sync, аудит оставшихся пар и пакетное слияние."""
from datetime import datetime

import pytest
from sqlalchemy import select

from app.dedup_audit import MERGE_SCORE, REPORT_SCORE, apply_merges, find_pairs
from app.ingest import merge_events
from app.models import Event
from app.sync import sync_records

DAY = datetime(2026, 11, 28, 19, 0)


def record(title, source, *, start=DAY, venue="Театр оперы и балета", **kw):
    return {"title": title, "start_at": start.isoformat(), "city": "Уфа", "venue": venue, "source": source, "url": f"https://{source}/{abs(hash((title, start))) % 99999}", **kw}


def test_same_event_from_three_sources_becomes_one_record(session):
    stats = sync_records(session, [
        record("Лебединое озеро", "culture.ru", price_min=500),
        record("Лебединое озеро. Балет П. И. Чайковского", "afisha.yandex.ru", price_min=700, price_max=3000, age_limit=6, venue="Башкирский театр оперы и балета"),
        record("Балет «Лебединое озеро»", "kassir.ru", price_min=600, description="Балет в трёх действиях"),
    ])
    events = list(session.scalars(select(Event)))
    assert len(events) == 1
    event = events[0]
    assert sorted(s["name"] for s in event.sources) == ["afisha.yandex.ru", "culture.ru", "kassir.ru"]
    assert (event.price_min, event.price_max, event.age_limit) == (500, 3000, 6)  # честный диапазон цен, максимум возраста
    assert event.description == "Балет в трёх действиях"
    assert (stats["created"], stats["cross_source"]) == (1, 2)  # две записи подтверждены другими источниками


def test_resync_of_the_same_source_is_an_update_not_a_cross_source_duplicate(session):
    batch = [record("Лебединое озеро", "kassir.ru")]
    sync_records(session, batch)
    again = sync_records(session, batch)
    assert again["cross_source"] == 0 and again["created"] == 0 and session.scalar(select(Event.id).limit(1)) is not None
    assert len(list(session.scalars(select(Event)))) == 1


def test_different_times_days_and_numbered_events_stay_separate(session):
    sync_records(session, [
        record("Золотой ключик", "culture.ru", start=datetime(2026, 11, 28, 11, 0)),
        record("Золотой ключик", "kassir.ru", start=datetime(2026, 11, 28, 16, 0)),  # дневной и вечерний сеансы
        record("Золотой ключик", "kassir.ru", start=datetime(2026, 11, 29, 11, 0)),  # другой день
        record("Лекция 1. Введение", "culture.ru", start=datetime(2026, 12, 1, 18, 0)),
        record("Лекция 2. Введение", "kassir.ru", start=datetime(2026, 12, 1, 18, 0)),
    ])
    assert len(list(session.scalars(select(Event)))) == 5


def test_dates_of_one_page_stay_separate_because_urls_differ(session):
    base = "https://afisha.yandex.ru/ufa/theatre_show/majsara"
    sync_records(session, [record("Проделки Майсары", "afisha.yandex.ru", start=datetime(2026, 11, 14, 18, 0), url=f"{base}#2026-11-14"),
                           record("Проделки Майсары", "afisha.yandex.ru", start=datetime(2027, 1, 7, 18, 0), url=f"{base}#2027-01-07")])
    assert len(list(session.scalars(select(Event)))) == 2


def raw(session, title, source, *, venue="Филармония", start=DAY, status="scheduled", price=None):
    event = Event(title=title, city="Уфа", venue=venue, start_at=start, category="concert", status=status, price_min=price,
                  sources=[{"name": source, "url": "", "status": status}], search_text=title.lower())
    session.add(event)
    session.commit()
    return event


def test_audit_reports_similar_pairs_that_ingest_kept_apart(session):
    a = raw(session, "Вечер органной музыки в филармонии", "culture.ru")
    b = raw(session, "Органная музыка: вечер в Башкирской филармонии", "kassir.ru")
    raw(session, "Сказка о рыбаке и рыбке", "culture.ru", venue="Театр кукол")
    pairs = find_pairs(session)
    assert [(p.keep_id, p.drop_id) for p in pairs] == [(a.id, b.id)]
    assert REPORT_SCORE <= pairs[0].score < 0.82 and pairs[0].score >= MERGE_SCORE


def test_audit_ignores_pairs_one_source_already_lists_separately_or_different_venues(session):
    raw(session, "Вечер органной музыки в филармонии", "culture.ru")
    raw(session, "Органная музыка: вечер в Башкирской филармонии", "culture.ru")  # тот же источник: это два события
    raw(session, "Вечер органной музыки в филармонии", "kassir.ru", venue="Дворец борьбы")  # площадка другая
    raw(session, "Органная музыка: вечер в Башкирской филармонии", "kassir.ru", start=datetime(2026, 11, 28, 11, 0))  # время далеко
    assert find_pairs(session) == []


def test_apply_merges_only_confident_pairs_and_keeps_the_worst_status(session):
    raw(session, "Вечер органной музыки в филармонии", "culture.ru", price=300)
    raw(session, "Органная музыка: вечер в Башкирской филармонии", "kassir.ru", status="cancelled", price=500)
    raw(session, "Лебединое озеро в театре", "culture.ru", venue="Опера", start=datetime(2026, 12, 3, 19, 0))
    raw(session, "Озеро лебедей балет вечер", "kassir.ru", venue="Опера", start=datetime(2026, 12, 3, 19, 0))
    pairs = find_pairs(session)
    merged = apply_merges(session, pairs, min_score=0.9)  # слишком строгий порог: ничего не сливается
    assert merged == 0 and len(list(session.scalars(select(Event)))) == 4
    assert apply_merges(session, pairs, min_score=0.72) >= 1
    organ = session.scalars(select(Event).where(Event.title.like("%орган%"))).all()
    assert len(organ) == 1
    assert organ[0].status == "cancelled" and sorted(s["name"] for s in organ[0].sources) == ["culture.ru", "kassir.ru"]
    assert (organ[0].price_min, organ[0].price_max) == (300, 500) or organ[0].price_min == 300


def test_merge_events_unions_sources_and_fills_gaps(session):
    keep = raw(session, "Концерт хора", "culture.ru", venue="")
    drop = raw(session, "Хор: концерт", "kassir.ru", venue="Филармония", price=900)
    drop.address, drop.description, drop.age_limit = "ул. Гоголя, 58", "Программа вечера", 12
    session.commit()
    merged = merge_events(session, keep, drop)
    assert merged.venue == "Филармония" and merged.address == "ул. Гоголя, 58" and merged.description == "Программа вечера"
    assert merged.age_limit == 12 and merged.price_min == 900 and len(merged.sources) == 2
    assert session.get(Event, drop.id) is None


@pytest.mark.parametrize("a, b", [("Лекция 1. Введение", "Лекция 2. Введение")])
def test_numbered_titles_are_never_reported(session, a, b):
    raw(session, a, "culture.ru")
    raw(session, b, "kassir.ru")
    assert find_pairs(session) == []
