"""Подписки на новые события (ТЗ §6.2): город и категория, отписка одной командой, без навязчивости."""
from datetime import datetime, timedelta

import pytest

from app import subscriptions as subs
from app.models import Event, Subscription

NOW = datetime(2026, 11, 10, 12, 0)  # вторник, день: рассылка разрешена
OLD = datetime(2026, 11, 9)  # подписались до появления новых событий


@pytest.fixture
def new_event(add, session):
    """Событие, добавленное в базу в заданный момент (created_at управляется вручную, а не часами теста)."""

    def make(title, *, city="Уфа", category="concert", created=datetime(2026, 11, 10, 10, 0), source="t1", **kw):
        event, _ = add(title, datetime(2026, 11, 12, 19, 0), city=city, category=category, source=source, **kw)
        event.created_at = created
        session.commit()
        return event

    return make


def subscribe(session, chat_id=1, city="", category="", checked=OLD):
    assert subs.add(session, chat_id, city, category) == "added"
    sub = subs.list_for(session, chat_id)[-1]
    sub.checked_at, sub.created_at = checked, checked
    session.commit()
    return sub


# ---------- управление подписками ----------

def test_add_list_duplicates_limit_and_unknown_category(session):
    assert subs.add(session, 1, "Уфа", "theatre") == "added"
    assert subs.add(session, 1, "Уфа", "theatre") == "exists"
    assert subs.add(session, 2, "Уфа", "theatre") == "added"  # у другого чата своя подписка
    for category in list(subs.CATEGORIES)[:subs.MAX_PER_CHAT - 1]:
        subs.add(session, 1, "Салават", category)
    assert len(subs.list_for(session, 1)) == subs.MAX_PER_CHAT
    assert subs.add(session, 1, "Сибай", "") == "limit"
    with pytest.raises(ValueError):
        subs.add(session, 3, "Уфа", "<script>")


def test_describe_is_readable():
    assert subs.describe(Subscription(city="Уфа", category="theatre")) == "Уфа · Театр"
    assert subs.describe(Subscription(city="", category="")) == "Вся республика · любая категория"


def test_unsubscribe_removes_everything_of_this_chat_only(session):
    subs.add(session, 1, "Уфа", "theatre")
    subs.add(session, 1, "", "concert")
    subs.add(session, 2, "Уфа", "")
    assert subs.remove_all(session, 1) == 2
    assert subs.list_for(session, 1) == [] and len(subs.list_for(session, 2)) == 1
    assert subs.remove_all(session, 1) == 0


def test_a_chat_cannot_delete_someone_elses_subscription(session):
    subs.add(session, 2, "Уфа", "")
    foreign = subs.list_for(session, 2)[0]
    assert subs.remove(session, 1, foreign.id) is False and len(subs.list_for(session, 2)) == 1
    assert subs.remove(session, 2, foreign.id) is True


def test_subscriptions_expire_after_the_retention_period(session):
    subs.add(session, 1, "Уфа", "")
    sub = subs.list_for(session, 1)[0]
    sub.created_at = NOW - timedelta(days=subs.RETENTION_DAYS + 1)
    session.commit()
    assert subs.purge_expired(session, NOW) == 1 and subs.list_for(session, 1) == []


# ---------- подборки ----------

def test_digest_contains_only_events_added_after_the_subscription_and_matching_it(session, new_event):
    subscribe(session, city="Уфа", category="concert")
    fresh = new_event("Новый концерт")
    new_event("Старый концерт", created=datetime(2026, 11, 8))  # был до подписки
    new_event("Театр в Уфе", category="theatre")  # другая категория
    new_event("Концерт в Салавате", city="Салават")  # другой город
    (digest,) = subs.due_digests(session, NOW)
    assert [e.id for e in digest.events] == [fresh.id] and digest.total == 1 and digest.chat_id == 1


def test_demo_events_never_go_to_subscribers(session, new_event):
    subscribe(session)
    new_event("Выдуманное", source="demo-afisha")
    assert subs.due_digests(session, NOW) == []


def test_at_most_one_digest_a_day_and_news_wait_until_tomorrow(session, new_event):
    subscribe(session)
    new_event("Первое")
    (digest,) = subs.due_digests(session, NOW)
    subs.mark_sent(session, digest, NOW)
    new_event("Второе", created=NOW + timedelta(minutes=5))
    assert subs.due_digests(session, NOW + timedelta(hours=3)) == []  # сегодня уже писали
    (tomorrow,) = subs.due_digests(session, NOW + timedelta(days=1))
    assert [e.title for e in tomorrow.events] == ["Второе"]  # «Первое» повторно не приходит


def test_nothing_is_sent_at_night(session, new_event):
    subscribe(session)
    new_event("Ночное", created=datetime(2026, 11, 10, 2, 0))
    assert subs.due_digests(session, datetime(2026, 11, 10, 3, 0)) == []
    assert subs.due_digests(session, datetime(2026, 11, 10, 21, 0)) == []
    assert subs.due_digests(session, datetime(2026, 11, 10, 9, 0))  # утром подборка уходит


def test_digest_is_capped_and_reports_the_total(session, new_event):
    subscribe(session)
    for i in range(subs.MAX_IN_DIGEST + 3):
        new_event(f"Концерт {i}")
    (digest,) = subs.due_digests(session, NOW)
    assert len(digest.events) == subs.MAX_IN_DIGEST and digest.total == subs.MAX_IN_DIGEST + 3


def test_without_news_the_checkpoint_moves_forward_and_nothing_is_sent(session, new_event):
    sub = subscribe(session)
    assert subs.due_digests(session, NOW) == []
    assert sub.checked_at == NOW and sub.last_sent_at is None


def test_several_subscriptions_of_one_chat_give_one_digest_without_repeats(session, new_event):
    subscribe(session, city="Уфа", category="concert")
    subscribe(session, city="", category="")
    new_event("Один на двоих")
    (digest,) = subs.due_digests(session, NOW)
    assert [e.title for e in digest.events] == ["Один на двоих"] and len(digest.sub_ids) == 2
    assert session.query(Event).count() == 1
