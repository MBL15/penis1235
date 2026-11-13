"""Журнал изменений, сырые данные источников, черновики, отчёт об импорте (ТЗ §6.1, §8.2)."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, select, text

from app import audit
from app.config import now_local
from app.db import add_missing_columns, backfill_defaults
from app.ingest import ingest_records, main as ingest_main, merge_events, parse_records
from app.models import AuditLog, Event, RawRecord
from app.pipeline import process_batch, process_record, reprocess_raw
from app.search import SearchFilters, facets, get_event, search
from app.sync import sync_records

from .test_api import payload
from .test_formatting_and_bot import _Ctx

START = (now_local() + timedelta(days=4)).replace(hour=19, minute=0, second=0, microsecond=0)


def rec(title="Квартет «Осень»", source="a.example", **kw):
    return {"title": title, "start_at": START.isoformat(), "source": source, "city": "Уфа", **kw}


def log_of(session, **filters):
    return audit.entries(session, **filters)


# ---------- журнал изменений ----------

def test_creating_through_the_api_is_logged_with_actor_and_request_id(client, admin_key, session):
    response = client.post("/api/v1/events", json=payload(), headers={**admin_key, "X-Request-ID": "trace-create-0001"})
    event_id = response.json()["event"]["id"]
    (entry,) = log_of(session)
    assert (entry.action, entry.actor, entry.event_id, entry.request_id) == ("create", "admin", event_id, "trace-create-0001")
    assert entry.changes["title"] == "Концерт симфонического оркестра" and entry.changes["source"] == "philharmonia.example"
    assert entry.at <= now_local()


def test_merge_and_update_log_only_what_changed(session):
    process_record(session, rec(price_min=300), actor="sync:a.example")
    process_record(session, rec(price_min=300, description="Подробный текст"), actor="sync:a.example")  # тот же источник уточнил
    process_record(session, rec(title="Квартет «Осень» (билеты)", source="b.example", price_max=900, status="sold_out"), actor="sync:b.example")
    created, updated, merged = reversed(log_of(session))
    assert created.action == "create"
    assert updated.action == "update" and updated.changes["description"] == ["", "Подробный текст"]
    assert "title" not in updated.changes and updated.actor == "sync:a.example"
    assert merged.action == "merge" and merged.changes["status"] == ["scheduled", "sold_out"]
    assert any(s["name"] == "b.example" for s in merged.changes["sources"][1])  # источники тоже попадают в журнал


def test_unchanged_repeat_writes_nothing_to_the_journal(session):
    process_record(session, rec())
    process_record(session, rec())
    assert len(log_of(session)) == 1


def test_status_patch_is_logged(client, admin_key, session):
    event_id = client.post("/api/v1/events", json=payload(), headers=admin_key).json()["event"]["id"]
    client.patch(f"/api/v1/events/{event_id}/status", json={"status": "cancelled"}, headers=admin_key)
    latest = log_of(session)[0]
    assert latest.action == "status" and latest.changes["status"] == ["scheduled", "cancelled"] and latest.actor == "admin"
    assert log_of(session, event_id=event_id)[0].id == latest.id and log_of(session, event_id=99999) == []


def test_dedup_merge_is_logged_for_both_events(session, add):
    keep, _ = add("Концерт", START, source="a")
    drop, _ = add("Совсем другой заголовок", START + timedelta(days=9), source="b")
    keep_id, drop_id = keep.id, drop.id
    merge_events(session, keep, drop, actor="dedup_audit")
    actions = {(e.action, e.event_id) for e in log_of(session) if e.actor == "dedup_audit"}
    assert actions == {("merge", keep_id), ("delete", drop_id)}


def test_long_texts_are_shortened_in_the_journal():
    changes = audit.diff({"description": ""}, {"description": "я" * 1000})
    assert len(changes["description"][1]) == 301 and changes["description"][1].endswith("…")


# ---------- сырые данные ----------

def test_every_record_is_kept_as_received_once_per_version(session):
    process_record(session, rec(price_min=300))
    process_record(session, rec(price_min=300))  # то же самое прислали снова
    (raw,) = session.scalars(select(RawRecord)).all()
    assert raw.source == "a.example" and raw.payload["price_min"] == 300 and raw.outcome == "unchanged" and raw.event_id
    assert raw.last_seen_at >= raw.first_seen_at
    process_record(session, rec(price_min=500))  # источник изменил запись: новая версия рядом со старой
    assert session.query(RawRecord).count() == 2


def test_rejected_records_are_kept_with_the_reason(session):
    result = process_record(session, {"title": "Без даты", "source": "bad.example"})
    assert result.status == "rejected" and any("start_at" in r and "обязательное поле" in r for r in result.reasons)
    (raw,) = session.scalars(select(RawRecord)).all()
    assert raw.outcome == "rejected" and "start_at" in raw.error and raw.event_id is None and session.query(Event).count() == 0


def test_normalization_can_be_replayed_from_raw_data_without_a_new_crawl(session):
    process_record(session, rec(title="Концерт «Осень»", price_min=300), actor="sync:a.example")
    process_record(session, rec(title="Концерт «Осень»", source="b.example", price_min=500, price_max=900), actor="sync:b.example")
    session.query(Event).delete()  # допустим, правила нормализации поменялись и данные пересобираются
    session.commit()
    report = reprocess_raw(session, actor="reprocess")
    assert report.created == 1 and report.updated == 1  # два источника снова склеились в одно событие
    (event,) = session.scalars(select(Event)).all()
    assert {s["name"] for s in event.sources} == {"a.example", "b.example"} and (event.price_min, event.price_max) == (300, 900)
    assert session.query(RawRecord).count() == 2  # сырые записи не размножились
    assert log_of(session)[0].actor == "reprocess"
    only_a = reprocess_raw(session, source="a.example")
    assert only_a.created == 0 and session.query(Event).count() == 1  # новых событий нет, прежнее обновилось


def test_sync_goes_through_the_same_pipeline(session):
    stats = sync_records(session, [rec(), {"title": "x"}], actor="sync:kassir.ru")
    assert (stats["created"], stats["skipped"]) == (1, 1)
    assert log_of(session)[0].actor == "sync:kassir.ru" and session.query(RawRecord).count() == 2


# ---------- черновики ----------

def test_drafts_are_invisible_to_the_public(session):
    result = process_record(session, rec(), publish=False)
    event = result.event
    assert event.publication == "draft"
    assert search(session, SearchFilters(include_past=True))[1] == 0
    assert search(session, SearchFilters(include_past=True, include_drafts=True))[1] == 1
    assert get_event(session, event.id) is None and get_event(session, event.id, include_drafts=True) is event
    assert facets(session)["cities"] == []


def test_a_draft_is_a_404_in_the_public_api_but_cancelled_stays_reachable(client, admin_key, session):
    draft = client.post("/api/v1/events/bulk?publish=false", json=[rec()], headers=admin_key).json()["events"][0]
    assert draft["publication"] == "draft"
    assert client.get(f"/api/v1/events/{draft['id']}").status_code == 404
    assert client.get("/api/v1/events", params={"include_past": True}).json()["total"] == 0
    published = client.post("/api/v1/events", json=payload(), headers=admin_key).json()["event"]
    client.patch(f"/api/v1/events/{published['id']}/status", json={"status": "cancelled"}, headers=admin_key)
    card = client.get(f"/api/v1/events/{published['id']}").json()  # отменённое доступно по прямой ссылке с пометкой
    assert card["status"] == "cancelled" and card["status_label"] == "Отменено" and card["publication"] == "published"


def test_a_draft_is_never_offered_by_the_agent(session):
    from app import agent

    process_record(session, rec(title="Секретный черновик"), publish=False)
    result = agent.ask(session, "что в Уфе", now=START - timedelta(days=1))
    assert not result.events


# ---------- отчёт об импорте ----------

def test_bulk_report_lists_rejected_records_and_does_not_fail_the_whole_batch(client, admin_key):
    batch = [rec("Первое"), {"title": "Без источника", "start_at": START.isoformat()}, rec("Первое"),
             rec("Третье", url="javascript:alert(1)"), "не объект", rec("Пятое", price_min=-5)]
    response = client.post("/api/v1/events/bulk", json=batch, headers=admin_key)
    assert response.status_code == 200
    data = response.json()
    assert (data["created"], data["updated"], data["unchanged"], data["total"]) == (1, 0, 1, 6)
    assert data["merged"] == data["updated"]  # прежнее имя поля
    rejected = {r["row"]: r for r in data["rejected"]}
    assert set(rejected) == {2, 4, 5, 6}
    assert "source: обязательное поле" in rejected[2]["reasons"] and rejected[2]["title"] == "Без источника"
    assert any("http" in reason for reason in rejected[4]["reasons"])
    assert rejected[5]["reasons"] == ["запись должна быть объектом с полями события"]
    assert any("price_min" in reason and "не меньше 0" in reason for reason in rejected[6]["reasons"])
    assert [e["title"] for e in data["events"]] == ["Первое"]


def test_import_is_idempotent_and_reports_updates(session):
    first = process_batch(session, [rec("Лекция 1"), rec("Лекция 2", price_min=0)], actor="import:a.json")
    again = process_batch(session, [rec("Лекция 1"), rec("Лекция 2", price_min=0)], actor="import:a.json")
    assert (first.created, again.created, again.unchanged, session.query(Event).count()) == (2, 0, 2, 2)
    updated = process_batch(session, [rec("Лекция 1", description="Теперь с описанием")], actor="import:a.json")
    assert updated.updated == 1 and updated.total == 1


def test_cli_prints_a_readable_report(session, tmp_path, monkeypatch, capsys):
    path = tmp_path / "events.csv"
    path.write_text(
        "title,start_at,source,city\n"
        f"Хороший вечер,{START.isoformat()},lib.example,Уфа\n"
        f"Без даты,,lib.example,Уфа\n",
        encoding="utf-8-sig")
    monkeypatch.setattr("app.db.SessionLocal", lambda: _Ctx(session))
    monkeypatch.setattr("app.db.init_db", lambda: None)
    assert ingest_main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "Создано: 1, обновлено: 0, без изменений: 0, отклонено: 1" in out
    assert "запись 3 «Без даты»: start_at: обязательное поле" in out  # в CSV первая строка данных — вторая строка файла
    assert log_of(session)[0].actor == "import:events.csv"
    assert ingest_main([str(path), "--draft"]) == 0  # повторный запуск: без дублей


def test_parse_records_explains_a_broken_file():
    with pytest.raises(ValueError, match="корректным JSON"):
        parse_records("{oops", "json")
    with pytest.raises(ValueError, match="список событий"):
        parse_records(json.dumps({"x": 1}), "json")
    with pytest.raises(ValueError, match="Неизвестный формат"):
        parse_records("<x/>", "xml")
    assert parse_records(json.dumps({"events": [rec()]}), ".json")[0]["title"] == "Квартет «Осень»"
    assert parse_records("﻿title,source\nA,b\n", "csv") == [{"title": "A", "source": "b"}]


def test_ingest_records_counts_rejections(session):
    stats = ingest_records(session, [rec("Хорошее"), {"title": "x"}])
    assert stats == {"created": 1, "merged": 0, "unchanged": 0, "rejected": 1}


# ---------- старая база ----------

def test_old_database_gets_publication_and_venue_key_filled():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE events (id INTEGER PRIMARY KEY, title VARCHAR(300), venue VARCHAR(200))"))
        conn.execute(text("INSERT INTO events (id, title, venue) VALUES (1, 'Старое', 'Дворец Молодёжи')"))
    added = add_missing_columns(engine)
    assert {"events.publication", "events.venue_key"} <= set(added)
    assert "publication" in {c["name"] for c in inspect(engine).get_columns("events")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT publication, venue_key FROM events")).one() == (None, None)
    assert backfill_defaults(engine) == 2
    assert backfill_defaults(engine) == 0  # повторный запуск ничего не меняет
    with engine.connect() as conn:
        assert conn.execute(text("SELECT publication, venue_key FROM events")).one() == ("published", "дворец молодежи")


def test_maintenance_reprocess_command(session, monkeypatch, capsys):
    from app.maintenance import main as maintenance_main

    process_record(session, rec())
    monkeypatch.setattr("app.db.SessionLocal", lambda: _Ctx(session))
    monkeypatch.setattr("app.db.init_db", lambda: None)
    assert maintenance_main(["reprocess"]) == 0
    assert "Сырых записей для переигрывания: 1" in capsys.readouterr().out
    assert maintenance_main(["reprocess", "--apply", "--source", "a.example"]) == 0
    assert "Создано: 0" in capsys.readouterr().out and session.query(Event).count() == 1
