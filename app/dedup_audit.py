"""Проверка базы на дубликаты, которые не склеились при приёме.

При приёме события склеиваются, если совпадают город и день, время близко, а заголовки похожи не меньше чем на 0,82.
Источники называют одно и то же по-разному («Лебединое озеро» и «Балет П. И. Чайковского «Лебединое озеро»»), поэтому здесь
порог мягче: пара попадает в отчёт, если заголовки похожи не меньше чем на 0,6, время близко, а площадки не противоречат
друг другу. Автоматически (--apply) сливаются только пары с сходством не ниже --min-score (по умолчанию 0,72).

  python -m app.dedup_audit                  # отчёт
  python -m app.dedup_audit --apply          # слить уверенные пары
  python -m app.dedup_audit --min-score 0.8 --apply
"""
import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .dedup import _time_close, _venues_conflict, similarity
from .ingest import merge_events
from .models import Event
from .normalize import norm_text, title_key

REPORT_SCORE = 0.6  # с какого сходства пара попадает в отчёт
MERGE_SCORE = 0.72  # с какого сходства --apply сливает пару сам


@dataclass
class Pair:
    keep_id: int
    drop_id: int
    score: float
    day: str
    keep_title: str
    drop_title: str
    keep_sources: list[str]
    drop_sources: list[str]
    venue: str


def _venue_ok(a: Event, b: Event) -> bool:
    """Площадки не противоречат: одна пуста, или совпадают, или делят слово («Конгресс-холл Торатау» и «Торатау»)."""
    if not a.venue or not b.venue:
        return True
    return not _venues_conflict(a.venue, b.venue) or norm_text(a.venue) == norm_text(b.venue)


def find_pairs(session: Session, min_score: float = REPORT_SCORE) -> list[Pair]:
    """Похожие записи одного города и дня, которые остались раздельными."""
    groups: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for event in session.scalars(select(Event)):
        groups[(event.city, event.start_at.date().isoformat())].append(event)
    pairs: list[Pair] = []
    for (_, day), events in groups.items():
        for a, b in combinations(sorted(events, key=lambda e: e.id), 2):
            if not _time_close(a.start_at, b.start_at) or not _venue_ok(a, b):
                continue
            if {x.get("name") for x in a.sources or []} & {x.get("name") for x in b.sources or []}:
                continue  # одна и та же площадка-источник уже считает их разными событиями
            score = similarity(title_key(a.title), title_key(b.title))
            if score < min_score:
                continue
            first, second = (a, b)
            pairs.append(Pair(first.id, second.id, round(score, 3), day, first.title, second.title,
                              [s.get("name", "") for s in first.sources or []], [s.get("name", "") for s in second.sources or []],
                              first.venue or second.venue or ""))
    return sorted(pairs, key=lambda p: -p.score)


def apply_merges(session: Session, pairs: list[Pair], min_score: float = MERGE_SCORE) -> int:
    """Сливает пары с достаточным сходством. Запись может входить в несколько пар: после слива пары с удалённой записью пропускаются."""
    removed: set[int] = set()
    merged = 0
    for pair in pairs:
        if pair.score < min_score or pair.drop_id in removed or pair.keep_id in removed:
            continue
        keep, drop = session.get(Event, pair.keep_id), session.get(Event, pair.drop_id)
        if keep is None or drop is None:
            continue
        merge_events(session, keep, drop)
        removed.add(pair.drop_id)
        merged += 1
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Найти (и при желании слить) дубликаты событий")
    parser.add_argument("--apply", action="store_true", help="слить пары с сходством не ниже --min-score")
    parser.add_argument("--min-score", type=float, default=MERGE_SCORE, help="порог автоматического слияния")
    parser.add_argument("--json", action="store_true", help="вывести отчёт в JSON")
    args = parser.parse_args(argv)
    try:  # кириллица в консоли Windows (cp1251/cp866)
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    from .db import SessionLocal, init_db

    init_db()
    with SessionLocal() as session:
        pairs = find_pairs(session)
        if args.json:
            print(json.dumps([asdict(p) for p in pairs], ensure_ascii=False, indent=1))
        else:
            print(f"Похожих пар: {len(pairs)} (порог отчёта {REPORT_SCORE}, автослияния {args.min_score})")
            for p in pairs[:60]:
                mark = "СЛИТЬ" if p.score >= args.min_score else "проверить"
                print(f"[{mark}] {p.score:.2f} {p.day} | #{p.keep_id} {p.keep_title[:50]!r} {p.keep_sources} <-> #{p.drop_id} {p.drop_title[:50]!r} {p.drop_sources} | {p.venue[:40]}")
        if args.apply:
            merged = apply_merges(session, pairs, args.min_score)
            print(f"Слито пар: {merged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
