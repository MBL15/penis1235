"""Сбор по расписанию (ТЗ §6.5, §14): регулярно забирает события из источников, проверяет их и помечает пропавшие.

Расписание лежит в data/schedule.json (или файл из --schedule). Задача типа sync забирает источник, кладёт новые события в очередь
проверки, публикует прошедшие проверку и (если включено mark_vanished) помечает пропавшие из источника. Задача типа verify
перепроверяет опубликованные события, которые давно не проверялись. Каждый запуск пишется в таблицу sync_runs.

  python -m app.scheduler                 # работать постоянно: раз в минуту смотреть, что пора запускать
  python -m app.scheduler --once          # запустить то, что пора, и выйти (для cron и Планировщика заданий Windows)
  python -m app.scheduler --list          # расписание и результат последних запусков
  python -m app.scheduler --run kassir    # запустить задачу сейчас, независимо от расписания

Один запуск задачи в один момент: незавершённый запуск младше двух часов блокирует новый. После сбоя повтор через час.
Сайты источников отвечают не всем серверам (в России, например, culture.ru): запускайте там, откуда они доступны."""
import argparse
import json
import logging
import sys
import time
from argparse import Namespace
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import vanished
from .config import now_local
from .models import Event, SyncRun
from .sources.base import BlockedError, SourceError

log = logging.getLogger("afisha.scheduler")

SCHEDULE_FILE = Path(__file__).resolve().parent.parent / "data" / "schedule.json"
RETRY_AFTER_FAILURE_HOURS = 1
STALE_RUNNING_HOURS = 2  # «идёт» дольше этого считаем зависшим
POLL_SECONDS = 60
KINDS = ("sync", "verify")


@dataclass
class Job:
    name: str
    kind: str = "sync"
    source: str = ""
    every_hours: float = 24.0
    args: dict = field(default_factory=dict)  # pages, limit, cities, delay, days для источника
    mark_vanished: bool = False
    links: bool = True  # проверять ссылки при проверке карточек


class ScheduleError(ValueError):
    pass


def load_schedule(path: Path = SCHEDULE_FILE) -> list[Job]:
    """Читает расписание и проверяет его: неверный файл должен сказать, что не так, а не молча ничего не запускать."""
    from .sync import SOURCES

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ScheduleError(f"нет файла расписания: {path}") from None
    except ValueError as exc:
        raise ScheduleError(f"расписание не читается как JSON: {exc}") from None
    if not isinstance(raw, list) or not raw:
        raise ScheduleError("расписание должно быть непустым списком задач")
    jobs, names = [], set()
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict) or not item.get("name"):
            raise ScheduleError(f"задача {i}: нужен объект с полем name")
        job = Job(name=str(item["name"])[:64], kind=item.get("kind", "sync"), source=item.get("source", ""),
                  every_hours=float(item.get("every_hours", 24)), args=dict(item.get("args", {})),
                  mark_vanished=bool(item.get("mark_vanished", False)), links=bool(item.get("links", True)))
        if job.kind not in KINDS:
            raise ScheduleError(f"задача «{job.name}»: kind должен быть один из {KINDS}")
        if job.kind == "sync" and job.source not in SOURCES:
            raise ScheduleError(f"задача «{job.name}»: неизвестный источник «{job.source}», допустимы {SOURCES}")
        if not 0.05 <= job.every_hours <= 24 * 30:
            raise ScheduleError(f"задача «{job.name}»: every_hours от 0,05 до 720")
        if job.name in names:
            raise ScheduleError(f"имя задачи «{job.name}» повторяется")
        names.add(job.name)
        jobs.append(job)
    return jobs


def last_run(session: Session, name: str) -> SyncRun | None:
    return session.scalar(select(SyncRun).where(SyncRun.job == name).order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(1))


def is_due(session: Session, job: Job, now: datetime) -> bool:
    last = last_run(session, job.name)
    if last is None:
        return True
    age = now - last.started_at
    if last.status == "running":
        return age >= timedelta(hours=STALE_RUNNING_HOURS)  # зависший запуск не блокирует навсегда
    if last.status == "failed":
        return age >= timedelta(hours=min(RETRY_AFTER_FAILURE_HOURS, job.every_hours))
    return age >= timedelta(hours=job.every_hours)


def _fetch_source(job: Job) -> list[dict]:
    from .sync import producers

    args = Namespace(pages=int(job.args.get("pages", 10)), limit=int(job.args.get("limit", 150)), cities=str(job.args.get("cities", "ufa")),
                     delay=float(job.args.get("delay", 1.5)), days=int(job.args.get("days", 3)))
    return list(producers(args)[job.source]())


def _count_vanished_queue(session: Session) -> int:
    return session.query(Event).filter(Event.publication == "draft", Event.review_reason.like(vanished.PREFIX + "%")).count()


def run_job(session: Session, job: Job, *, fetch: Callable[[Job], list[dict]] | None = None, opener=None, pause: float = 1.0) -> SyncRun:
    """Выполняет задачу и записывает результат в sync_runs. Исключения наружу не выходят: сбой источника записывается как failed."""
    from .sync import sync_records
    from .verify import stale, verify_events, verify_pending

    started = now_local()
    run = SyncRun(job=job.name, started_at=started, status="running")
    session.add(run)
    session.commit()
    try:
        if job.kind == "verify":
            pending = verify_pending(session, links=job.links, pause=pause, opener=opener)  # сначала очередь, потом давно не проверявшиеся
            events = stale(session, int(job.args.get("stale_days", 14)), int(job.args.get("limit", 200)))
            report = verify_events(session, events, links=job.links, pause=pause, opener=opener)
            run.fetched, run.published, run.queued = report.checked + pending.checked, pending.published, pending.queued + report.demoted
        else:
            records = (fetch or _fetch_source)(job)
            run.fetched = len(records)
            before = _count_vanished_queue(session)
            stats = sync_records(session, records, actor=f"sync:{job.name}", queue=True)
            run.created, run.merged, run.unchanged, run.skipped = stats["created"], stats["merged"], stats["unchanged"], stats["skipped"]
            report = verify_pending(session, links=job.links, pause=pause, opener=opener)
            run.published, run.queued = report.published, report.queued
            run.revived = max(0, before - _count_vanished_queue(session))
            if job.mark_vanished:
                # Сравниваем с лучшим из последних проходов, а не с предыдущим: пустой «успешный» проход не должен занижать планку
                recent = session.scalars(select(SyncRun.fetched).where(SyncRun.job == job.name, SyncRun.status == "ok", SyncRun.id != run.id)
                                         .order_by(SyncRun.started_at.desc()).limit(5)).all()
                previous = max(recent, default=0)
                if not records or (previous and len(records) < previous * vanished.MIN_SHARE):
                    run.error = f"пропавшие не помечались: источник прислал {len(records)} записей против {previous} в прошлый раз"
                else:
                    for source in sorted({str(r.get("source", "")) for r in records if r.get("source")}):
                        run.vanished += vanished.mark_vanished(session, source, started, actor=f"sync:{job.name}")
        run.status = "ok"
    except (BlockedError, SourceError) as exc:
        run.status, run.error = "failed", f"источник: {exc}"[:400]
        log.warning("Задача %s: %s", job.name, run.error)
    except Exception as exc:  # одна упавшая задача не должна останавливать расписание
        session.rollback()
        run.status, run.error = "failed", f"{type(exc).__name__}: {exc}"[:400]
        log.exception("Задача %s упала", job.name)
    run.finished_at = now_local()
    session.merge(run)
    session.commit()
    return run


def run_due(session_factory, jobs: list[Job], *, now: datetime | None = None, fetch=None, opener=None, pause: float = 1.0) -> list[SyncRun]:
    """Запускает задачи, которым пора, по очереди. Возвращает записи запусков."""
    now = now or now_local()
    done = []
    for job in jobs:
        with session_factory() as session:
            if is_due(session, job, now):
                done.append(run_job(session, job, fetch=fetch, opener=opener, pause=pause))
    return done


def describe_runs(session: Session, jobs: list[Job], now: datetime | None = None) -> list[str]:
    now = now or now_local()
    lines = []
    for job in jobs:
        last = last_run(session, job.name)
        when = "ни разу не запускалась" if last is None else (
            f"{last.started_at:%d.%m %H:%M}, {last.status}: получено {last.fetched}, создано {last.created}, обновлено {last.merged}, "
            f"опубликовано {last.published}, в очереди {last.queued}, пропало {last.vanished}, вернулось {last.revived}"
            + (f", {last.error}" if last.error else ""))
        lines.append(f"{job.name} (раз в {job.every_hours:g} ч, {'пора' if is_due(session, job, now) else 'ждёт'}): {when}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сбор по расписанию")
    parser.add_argument("--schedule", type=Path, default=SCHEDULE_FILE, help="файл расписания (по умолчанию data/schedule.json)")
    parser.add_argument("--once", action="store_true", help="запустить то, что пора, и выйти")
    parser.add_argument("--list", action="store_true", help="показать расписание и последние запуски")
    parser.add_argument("--run", metavar="ЗАДАЧА", help="запустить задачу сейчас")
    parser.add_argument("--pause", type=float, default=1.0, help="пауза между запросами к одному сайту при проверке ссылок, секунд")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    try:
        jobs = load_schedule(args.schedule)
    except ScheduleError as exc:
        print(f"Расписание: {exc}", file=sys.stderr)
        return 2
    from .db import SessionLocal, init_db

    init_db()
    if args.list:
        with SessionLocal() as session:
            print("\n".join(describe_runs(session, jobs)))
        return 0
    if args.run:
        job = next((j for j in jobs if j.name == args.run), None)
        if job is None:
            print(f"Нет задачи «{args.run}». Есть: {', '.join(j.name for j in jobs)}", file=sys.stderr)
            return 2
        with SessionLocal() as session:
            run = run_job(session, job, pause=args.pause)
        print(f"{job.name}: {run.status}; получено {run.fetched}, создано {run.created}, опубликовано {run.published}, "
              f"в очереди {run.queued}, пропало {run.vanished}" + (f"; {run.error}" if run.error else ""))
        return 0 if run.status == "ok" else 1
    while True:
        for run in run_due(SessionLocal, jobs, pause=args.pause):
            log.info("%s: %s (получено %d, создано %d, пропало %d) %s", run.job, run.status, run.fetched, run.created, run.vanished, run.error)
        if args.once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
