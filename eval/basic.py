"""Базовое качество (ТЗ §9.1, уровни 1–2): заполненность карточек, дубли в выдаче, полнота поиска, пустая выдача, скорость
поиска без модели и импорт файла на 100 событий. Без сети и модели, на той же базе, что и прогон контрольных запросов
(`eval/dataset.py`: data/sample_events.json + записи для проверок, «сейчас» зафиксировано).

    python -m eval.basic                 # отчёт в eval/reports/<дата>-basic.md и .json
    python -m eval.basic --no-write      # только вывести

Что считается обязательными полями карточки — REQUIRED ниже (ТЗ §6.2: название, категория, дата и время, город и площадка,
ссылка на источник). Адрес, описание, цена и возраст обязательными не считаются: источники их часто не дают, они выводятся
отдельной строкой как «спорные» (ТЗ §6.1: «спорные помечены»)."""
import argparse
import json
import sys
import time as clock
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Event
from app.normalize import norm_text
from app.pipeline import process_batch
from app.search import SearchFilters, _conditions, search

from . import checks
from .dataset import NOW, SAMPLE, build_session
from .run import REPORTS, agent_mode, ask_dialog, load_queries, percentile

SEARCH_KINDS = ("search", "typo", "followup", "similar")
REQUIRED = {
    "название": lambda e: bool((e.title or "").strip()),
    "категория": lambda e: bool(e.category),
    "дата и время": lambda e: e.start_at is not None,
    "город": lambda e: bool((e.city or "").strip()),
    "площадка": lambda e: bool((e.venue or "").strip()),
    "ссылка на источник": lambda e: bool(e.url or any(s.get("url") for s in e.sources or [])),
}
OPTIONAL = {
    "адрес": lambda e: bool((e.address or "").strip()),
    "описание": lambda e: bool((e.description or "").strip()),
    "цена или «бесплатно»": lambda e: e.price_min is not None,
    "возраст": lambda e: e.age_limit is not None,
}
THRESHOLDS = {"fill": 0.95, "dups": 0.02, "recall": 0.90, "empty": 0.05, "p95_s": 1.5}


@dataclass
class Metric:
    name: str
    value: str
    threshold: str
    ok: bool | None
    detail: str


def _key(e: Event) -> tuple:
    return norm_text(e.title), e.start_at.date(), e.venue_key or norm_text(e.venue)


def current_events(session) -> list[Event]:
    return list(session.scalars(select(Event).where(*_conditions(SearchFilters(), NOW))))


def fill_rate(session) -> tuple[Metric, dict]:
    events = current_events(session)
    full = [e for e in events if all(check(e) for check in REQUIRED.values())]
    missing = Counter(name for e in events for name, check in REQUIRED.items() if not check(e))
    optional = {name: sum(check(e) for e in events) for name, check in OPTIONAL.items()}
    share = len(full) / len(events) if events else 0.0
    detail = f"{len(full)} из {len(events)} актуальных событий"
    if missing:
        detail += "; не хватает: " + ", ".join(f"{k} у {v}" for k, v in missing.most_common())
        fixtures = sum(1 for e in events if e not in full and any(s.get("name") == "eval-fixture" for s in e.sources or []))
        if fixtures:
            detail += f" (из них {fixtures} — искусственные записи для проверок из eval/dataset.py)"
    extra = {name: f"{n} из {len(events)}" for name, n in optional.items()}
    return Metric("Заполненность карточек", f"{share:.0%}", "≥ 95 %", share >= THRESHOLDS["fill"], detail), extra


def search_quality(session) -> tuple[list[Metric], dict]:
    """Полнота, пустая выдача и дубли — на поисковых запросах набора; скорость — на всех ответах режима без модели."""
    queries = [q for q in load_queries() if q["kind"] in SEARCH_KINDS and q.get("oracle") is not None]
    all_queries = load_queries()
    hits = empties = with_data = shown = duplicate_cards = 0
    times: list[float] = []
    with agent_mode("rules"):
        for q in all_queries:
            turns = ask_dialog(session, q, None, 0.0)
            times += [t.wall_ms / 1000 for t in turns]
            if q not in queries:
                continue
            _, oracle_total = search(session, checks.oracle_filters(q["oracle"]), NOW)
            events = turns[-1].events
            shown += len(events)
            duplicate_cards += len(events) - len({_key(e) for e in events})
            if not oracle_total:
                continue
            with_data += 1
            relevant = [e for e in events if not checks.violations(e, q.get("constraints", {}))]
            hits += bool(relevant)
            empties += not events
    recall = hits / with_data if with_data else 0.0
    empty = empties / with_data if with_data else 0.0
    dups = duplicate_cards / shown if shown else 0.0
    p95 = percentile(times, 0.95)
    metrics = [
        Metric("Дубли в выдаче", f"{dups:.1%}", "≤ 2 %", dups <= THRESHOLDS["dups"],
               f"{duplicate_cards} повторов на {shown} показанных карточек (ключ: название, день, площадка)"),
        Metric("Полнота поиска", f"{recall:.0%}", "≥ 90 %", recall >= THRESHOLDS["recall"],
               f"{hits} из {with_data} запросов с данными: показано хотя бы одно событие без нарушения условий"),
        Metric("Пустая выдача", f"{empty:.0%}", "≤ 5 %", empty <= THRESHOLDS["empty"],
               f"{empties} из {with_data} запросов с данными вернули пусто"),
        Metric("Скорость поиска (p95)", f"{p95:.3f} с", "≤ 1,5 с", p95 <= THRESHOLDS["p95_s"],
               f"по {len(times)} ответам режима без модели (разбор запроса, поиск, запись трассы), максимум {max(times, default=0):.3f} с"),
    ]
    return metrics, {"queries_with_data": with_data}


def import_hundred() -> Metric:
    """100 разных событий из образца в пустую базу, затем тот же файл ещё раз: дублей и ручной правки быть не должно."""
    records, seen = [], set()
    for record in json.loads(SAMPLE.read_text(encoding="utf-8")):
        key = (norm_text(record.get("title", "")), str(record.get("start_at", ""))[:16])
        if key not in seen:
            seen.add(key)
            records.append(record)
        if len(records) == 100:
            break
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(engine, expire_on_commit=False)() as session:
        first = process_batch(session, records, actor="eval-import")
        events_after_first = session.query(Event).count()
        second = process_batch(session, records, actor="eval-import")
        events_after_second = session.query(Event).count()
    ok = (not first.rejected and second.created == 0 and second.updated == 0 and events_after_first == events_after_second)
    detail = (f"{len(records)} записей: создано {first.created}, слито с уже загруженными {first.updated}, отклонено {len(first.rejected)}; "
              f"повторная загрузка: создано {second.created}, обновлено {second.updated}, без изменений {second.unchanged}; "
              f"событий в базе {events_after_first} → {events_after_second}")
    return Metric("Импорт 100 событий", "100 %" if ok else "не выполнено", "100 %", ok, detail)


def run() -> dict:
    session = build_session()
    fill, optional = fill_rate(session)
    search_metrics, extra = search_quality(session)
    metrics = [fill, *search_metrics, import_hundred()]
    return {"date": datetime.now().strftime("%Y-%m-%d %H:%M"), "now": NOW.isoformat(sep=" "), "metrics": [m.__dict__ for m in metrics],
            "optional_fields": optional, **extra}


def render(report: dict) -> str:
    mark = {True: "✅ выполнен", False: "❌ не выполнен", None: "➖"}
    lines = ["# Базовое качество (ТЗ §9.1)", "",
             f"Дата прогона: {report['date']}. База: `data/sample_events.json` и записи для проверок (`eval/dataset.py`), «сейчас» = {report['now']}. "
             "Без модели и сети: `python -m eval.basic`.", "",
             "| Метрика | Значение | Порог | Итог | Как посчитано |", "|---|---|---|---|---|"]
    lines += [f"| {m['name']} | {m['value']} | {m['threshold']} | {mark[m['ok']]} | {m['detail']} |" for m in report["metrics"]]
    failed = [m["name"] for m in report["metrics"] if m["ok"] is False]
    lines += ["", f"Порогов не выполнено: **{len(failed)}**" + (f" ({', '.join(failed)})." if failed else "."), "",
              "Необязательные поля у актуальных событий (заполнено): " + "; ".join(f"{k} — {v}" for k, v in report["optional_fields"].items()) + ".", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Базовое качество (ТЗ §9.1)")
    parser.add_argument("--no-write", action="store_true", help="не записывать отчёт в eval/reports")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    started = clock.perf_counter()
    report = run()
    text = render(report)
    print(text)
    if not args.no_write:
        stem = Path(REPORTS) / f"{datetime.now():%Y-%m-%d}-basic"
        stem.with_suffix(".md").write_text(text, encoding="utf-8")
        stem.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Отчёт: {stem}.md, {stem}.json ({clock.perf_counter() - started:.1f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
