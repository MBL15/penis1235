"""Сбор по расписанию: расписание, очерёдность запусков, запись результата, пропавшие из источника события и их возврат."""
import contextlib
import json
import socket
import urllib.error
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import scheduler, vanished
from app.config import now_local
from app.models import Event, SyncRun
from app.scheduler import Job, ScheduleError, describe_runs, is_due, load_schedule, run_due, run_job
from app.search import SearchFilters, search
from app.sources.base import BlockedError, SourceError

NOW = now_local().replace(hour=9, minute=0, second=0, microsecond=0)
START = NOW + timedelta(days=3, hours=10)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, type=0, **kw: [(2, 1, 6, "", ("93.184.216.34", port))])


def ok_opener(request, timeout):
    return SimpleNamespace(status=200, getcode=lambda: 200, read=lambda n=0: b"")


def record(title, n=1, source="kassir.ru"):
    return {"title": title, "start_at": (START + timedelta(hours=n)).isoformat(), "city": "Уфа", "venue": f"Зал {title}", "address": "ул. Ленина, 5",
            "price_min": 300, "age_limit": 6, "description": "Подробное описание события для карточки афиши.",
            "url": f"https://example.org/{source}/{sum(map(ord, title))}", "source": source}


def age_sources(session, hours):
    """Сдвигает «видели» назад: имитирует прошедшее между запусками время (запуски в тесте идут в одну секунду)."""
    for event in session.query(Event).all():
        event.sources = [{**s, "seen_at": (now_local() - timedelta(hours=hours)).isoformat(timespec="seconds")} for s in event.sources]
    session.commit()


JOB = Job(name="kassir", source="kassir", every_hours=12, mark_vanished=True)


def run(session, records, job=JOB):
    return run_job(session, job, fetch=lambda j: records, opener=ok_opener, pause=0)


# ---------- расписание ----------

def test_default_schedule_file_is_valid():
    jobs = load_schedule()
    assert {j.name for j in jobs} >= {"culture-ru", "kassir", "verify"} and any(j.kind == "verify" for j in jobs)


def test_bad_schedules_say_what_is_wrong(tmp_path):
    def attempt(content):
        path = tmp_path / "s.json"
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        with pytest.raises(ScheduleError) as err:
            load_schedule(path)
        return str(err.value)

    assert "JSON" in attempt("{нет")
    assert "непустым списком" in attempt([])
    assert "полем name" in attempt([{"source": "kassir"}])
    assert "неизвестный источник" in attempt([{"name": "x", "source": "nope"}])
    assert "every_hours" in attempt([{"name": "x", "source": "kassir", "every_hours": 0}])
    assert "повторяется" in attempt([{"name": "x", "source": "kassir"}, {"name": "x", "source": "kudago"}])
    assert "kind" in attempt([{"name": "x", "kind": "magic"}])
    with pytest.raises(ScheduleError):
        load_schedule(tmp_path / "нет.json")


def test_due_logic_first_run_interval_running_and_failure_retry(session):
    job = Job(name="a", source="kassir", every_hours=12)
    now = now_local()
    assert is_due(session, job, now)  # ни разу не запускалась
    session.add(SyncRun(job="a", started_at=now - timedelta(hours=5), status="ok"))
    session.commit()
    assert not is_due(session, job, now) and is_due(session, job, now + timedelta(hours=8))
    session.add(SyncRun(job="a", started_at=now - timedelta(minutes=30), status="running"))
    session.commit()
    assert not is_due(session, job, now)  # идёт сейчас: второй запуск не стартует
    assert is_due(session, job, now + timedelta(hours=3))  # зависший (больше двух часов) не блокирует навсегда
    session.add(SyncRun(job="b", started_at=now - timedelta(minutes=20), status="failed", error="сеть"))
    session.commit()
    failed = Job(name="b", source="kassir", every_hours=12)
    assert not is_due(session, failed, now) and is_due(session, failed, now + timedelta(hours=1, minutes=5))  # повтор через час, а не через 12


# ---------- запуск ----------

def test_run_collects_verifies_publishes_and_records_the_result(session):
    result = run(session, [record("Концерт А", 1), record("Концерт Б", 2)])
    assert result.status == "ok" and (result.fetched, result.created, result.published, result.queued) == (2, 2, 2, 0)
    assert result.finished_at is not None and session.query(SyncRun).count() == 1
    assert search(session, SearchFilters(), NOW)[1] == 2
    again = run(session, [record("Концерт А", 1), record("Концерт Б", 2)])
    assert (again.created, again.unchanged) == (0, 2) and session.query(Event).count() == 2


def test_failed_source_is_recorded_and_does_not_touch_data(session):
    run(session, [record("Концерт А")])

    def blocked(job):
        raise BlockedError("сайт просит остановиться")

    failed = run_job(session, JOB, fetch=blocked, opener=ok_opener, pause=0)
    assert failed.status == "failed" and "остановиться" in failed.error
    assert session.query(Event).filter(Event.publication == "published").count() == 1  # данные целы
    crashed = run_job(session, JOB, fetch=lambda j: 1 / 0, opener=ok_opener, pause=0)
    assert crashed.status == "failed" and "ZeroDivisionError" in crashed.error


def test_run_due_runs_only_what_is_due(session):
    other = Job(name="other", source="culture-ru", every_hours=24)
    factory = lambda: contextlib.nullcontext(session)  # noqa: E731
    done = run_due(factory, [JOB, other], fetch=lambda j: [record(j.name)], opener=ok_opener, pause=0)
    assert [r.job for r in done] == ["kassir", "other"]
    assert run_due(factory, [JOB, other], fetch=lambda j: [], opener=ok_opener, pause=0) == []  # только что запускались


# ---------- пропавшие из источника ----------

def test_event_missing_from_the_source_goes_to_moderation_and_returns_when_it_comes_back(session):
    run(session, [record("Остаётся", 1), record("Пропадёт", 2)])
    age_sources(session, hours=13)
    gone = run(session, [record("Остаётся", 1)])
    assert gone.vanished == 1 and "Пропадёт" not in [e.title for e in search(session, SearchFilters(), NOW)[0]]
    event = session.query(Event).filter(Event.title == "Пропадёт").one()
    assert event.publication == "draft" and event.review_reason.startswith(vanished.PREFIX) and "kassir.ru" in event.review_reason
    assert event.sources[0].get("vanished_at")
    # «пропало» решает человек: автоматическая проверка такую карточку не публикует
    run(session, [])
    assert session.query(Event).filter(Event.title == "Пропадёт").one().publication == "draft"
    age_sources(session, hours=13)
    back = run(session, [record("Остаётся", 1), record("Пропадёт", 2)])
    assert back.revived == 1
    event = session.query(Event).filter(Event.title == "Пропадёт").one()
    assert event.publication == "published" and event.review_reason is None and not event.sources[0].get("vanished_at")


def test_empty_or_tiny_response_never_removes_the_whole_listing(session):
    run(session, [record(f"Событие {i}", i) for i in range(1, 7)])
    age_sources(session, hours=13)
    empty = run(session, [])
    assert empty.vanished == 0 and session.query(Event).filter(Event.publication == "published").count() == 6
    tiny = run(session, [record("Событие 1", 1)])  # 1 из 6: меньше половины, значит, скорее сбой сбора
    assert tiny.vanished == 0 and "пропавшие не помечались" in tiny.error
    assert session.query(Event).filter(Event.publication == "published").count() == 6


def test_event_confirmed_by_another_source_stays_published(session):
    run(session, [record("Общее событие", 1, source="kassir.ru")])
    run_job(session, Job(name="yandex", source="yandex-afisha"), fetch=lambda j: [record("Общее событие", 1, source="afisha.yandex.ru")], opener=ok_opener, pause=0)
    age_sources(session, hours=13)
    run(session, [record("Другое", 3)])
    event = session.query(Event).filter(Event.title == "Общее событие").one()
    assert event.publication == "published"  # у kassir.ru пропало, но Яндекс Афиша его подтверждает
    assert [bool(s.get("vanished_at")) for s in event.sources if s["name"] == "kassir.ru"] == [True]


def test_past_events_are_not_marked_as_vanished(session):
    run(session, [record("Прошедшее", 1), record("Будущее", 2)])
    old = session.query(Event).filter(Event.title == "Прошедшее").one()
    old.start_at = NOW - timedelta(days=3)
    session.commit()
    age_sources(session, hours=13)
    result = run(session, [record("Будущее", 2)])
    assert result.vanished == 0


# ---------- перепроверка и вывод ----------

def test_verify_job_rechecks_published_events_and_takes_down_dead_links(session):
    run(session, [record("Концерт А", 1)])

    def dead(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "x", {}, None)

    for e in session.query(Event).all():
        e.verified_at = None
    session.commit()
    result = run_job(session, Job(name="verify", kind="verify"), opener=dead, pause=0)
    assert result.status == "ok" and result.fetched == 1 and result.queued == 1
    event = session.query(Event).one()
    assert event.publication == "draft" and "ссылка не открывается" in event.review_reason


def test_describe_runs_and_cli_list(session, monkeypatch, tmp_path, capsys):
    run(session, [record("Концерт А")])
    lines = describe_runs(session, [JOB, Job(name="never", source="culture-ru")])
    assert "kassir (раз в 12 ч, ждёт)" in lines[0] and "создано 1" in lines[0] and "ни разу не запускалась" in lines[1]
    path = tmp_path / "s.json"
    path.write_text(json.dumps([{"name": "x", "source": "kassir"}]), encoding="utf-8")
    monkeypatch.setattr("app.db.init_db", lambda: None)
    monkeypatch.setattr("app.db.SessionLocal", lambda: contextlib.nullcontext(session))
    assert scheduler.main(["--schedule", str(path), "--list"]) == 0 and "x (раз в 24 ч" in capsys.readouterr().out
    assert scheduler.main(["--schedule", str(path), "--run", "нет"]) == 2
    assert scheduler.main(["--schedule", str(tmp_path / "нет.json"), "--list"]) == 2


def test_admin_sees_sync_runs(client, session, admin_key):
    run(session, [record("Концерт А")])
    denied = client.get("/api/v1/admin/sync-runs")
    assert denied.status_code in (401, 403)
    runs = client.get("/api/v1/admin/sync-runs", headers=admin_key).json()
    assert runs[0]["job"] == "kassir" and runs[0]["status"] == "ok" and runs[0]["created"] == 1 and runs[0]["fetched"] == 1
    assert client.get("/api/v1/admin/sync-runs?job=nope", headers=admin_key).json() == []


def test_only_jobs_filters_and_rejects_typos():
    """Сервер: только доступные источники (deploy/run.sh запускает кино и проверку). Опечатка не выключает сбор молча."""
    jobs = load_schedule()
    picked = scheduler.only_jobs(jobs, "afisha-cinema, verify")
    assert [j.name for j in picked] == ["afisha-cinema", "verify"]
    assert scheduler.only_jobs(jobs, "") == jobs and scheduler.only_jobs(jobs, None) == jobs
    with pytest.raises(ScheduleError, match="kino"):
        scheduler.only_jobs(jobs, "afisha-cinema,kino")


def test_server_schedule_has_the_cinema_job():
    cinema = next(j for j in load_schedule() if j.name == "afisha-cinema")
    from app.sources import afisha_cinema

    assert cinema.source == "afisha-cinema" and cinema.every_hours <= 12
    assert set(cinema.args["cities"].split(",")) <= set(afisha_cinema.CITY_SLUGS)


def test_deploy_runs_the_scheduler_with_cinema_only():
    from pathlib import Path

    run_sh = (Path(__file__).resolve().parent.parent / "deploy" / "run.sh").read_text(encoding="utf-8")
    assert "--name afisha-scheduler" in run_sh and "python -m app.scheduler" in run_sh
    assert 'SCHEDULE_ONLY="${SCHEDULE_ONLY-afisha-cinema,verify}"' in run_sh
