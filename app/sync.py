"""Синхронизация с внешними источниками.

  python -m app.sync culture-ru --pages 10          # федеральная афиша culture.ru
  python -m app.sync yandex-afisha --limit 150      # Яндекс Афиша (страницы событий по sitemap)
  python -m app.sync kassir                         # Kassir.ru: разделы афиши города
  python -m app.sync kudago                         # KudaGo (официальный API; из России бывает недоступен)
  python -m app.sync afisha-cinema --days 3         # кино на Афише (afisha.ru): кинотеатры города и их сеансы
  python -m app.sync sites                          # сайты из data/sources.json (schema.org)
  python -m app.sync all --limit 100                # все источники подряд, сбой одного не останавливает остальные
  python -m app.sync all --dry-run                  # показать, что загрузится, ничего не записывая

После загрузки выводится сводка по дубликатам: сколько событий склеено с уже известными (в том числе подтверждённых
другим источником) и сколько похожих пар осталось для проверки (python -m app.dedup_audit).
"""
import argparse
import json
import logging
import sys
from collections import Counter
from collections.abc import Callable, Iterable

from .pipeline import process_record
from .verify import verify_pending
from .sources import afisha_cinema, culture_ru, kassir, kudago, sites, yandex_afisha
from .sources.base import BlockedError, SourceError

SOURCES = ("culture-ru", "yandex-afisha", "kassir", "kudago", "afisha-cinema", "sites")


def sync_records(session, records, *, actor: str = "sync", queue: bool = False) -> dict[str, int]:
    """Как ingest_records, но битая запись не обрывает всю загрузку.

    created: новое событие; merged: то же событие обновлено (в том числе уточнено другим источником);
    cross_source: из merged — события, которые подтвердил ДРУГОЙ источник (дубли между источниками найдены и склеены);
    unchanged: ничего нового; skipped: запись не прошла проверку."""
    stats = {"created": 0, "merged": 0, "unchanged": 0, "skipped": 0, "cross_source": 0}
    for record in records:
        result = process_record(session, record, actor=actor, publish=not queue, auto_queue=queue)  # сырая запись и журнал пишутся внутри
        if result.status == "rejected":
            stats["skipped"] += 1
            print(f"пропущено: {record.get('title')!r}: {result.reasons[0]}", file=sys.stderr)
            continue
        stats[result.status] += 1
        stats["cross_source"] += int(result.cross_source)
    return stats


def producers(args) -> dict[str, Callable[[], Iterable[dict]]]:
    cities = {slug.strip(): yandex_afisha.CITY_SLUGS[slug.strip()] for slug in args.cities.split(",") if slug.strip() in yandex_afisha.CITY_SLUGS}
    return {
        "culture-ru": lambda: culture_ru.fetch_records(args.pages, args.delay),
        "yandex-afisha": lambda: yandex_afisha.fetch_records(args.limit, cities=cities or None),
        "kassir": lambda: kassir.fetch_records(cities={s: kassir.CITIES[s] for s in args.cities.split(",") if s in kassir.CITIES} or None),
        "kudago": lambda: kudago.fetch_records(locations={s: kudago.LOCATIONS[s] for s in args.cities.split(",") if s in kudago.LOCATIONS} or None),
        "afisha-cinema": lambda: afisha_cinema.fetch_records(
            cities={s.strip(): afisha_cinema.CITY_SLUGS[s.strip()] for s in args.cities.split(",") if s.strip() in afisha_cinema.CITY_SLUGS} or None,
            days=getattr(args, "days", 3)),
        "sites": lambda: sites.fetch_records(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Загрузить события из внешних источников")
    parser.add_argument("source", choices=[*SOURCES, "all"])
    parser.add_argument("--pages", type=int, default=10, help="culture.ru: сколько страниц афиши обойти (по 20 событий)")
    parser.add_argument("--limit", type=int, default=150, help="Яндекс Афиша: сколько страниц событий обойти за запуск")
    parser.add_argument("--cities", default="ufa", help="города через запятую (ufa,sterlitamak,...)")
    parser.add_argument("--days", type=int, default=3, help="кино на Афише: на сколько дней вперёд брать расписание (включая сегодня)")
    parser.add_argument("--delay", type=float, default=1.5, help="culture.ru: пауза между страницами, секунд")
    parser.add_argument("--dry-run", action="store_true", help="ничего не записывать, показать первые записи")
    parser.add_argument("--drop-demo", action="store_true", help="удалить демо-события перед загрузкой")
    parser.add_argument("--no-verify", action="store_true", help="публиковать новые события сразу, без проверки (по умолчанию они ждут проверки)")
    parser.add_argument("--no-links", action="store_true", help="проверка без обхода ссылок: только дата, полнота и дубли")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Кириллица в консоли Windows: без этого печать падает на кодировке cp1251/cp866.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    names = list(SOURCES) if args.source == "all" else [args.source]
    if args.source == "all" and not sites.load_sites():
        names.remove("sites")  # список сайтов пуст: молча пропускаем
    make = producers(args)

    if not args.dry_run:
        from .db import SessionLocal, init_db
        from .maintenance import reset_demo

        init_db()
    failed = 0
    totals: Counter = Counter()
    for name in names:
        try:
            records = list(make[name]())
        except BlockedError as exc:
            print(f"[{name}] сайт просит остановиться: {exc}", file=sys.stderr)
            failed += 1
            continue
        except SourceError as exc:
            print(f"[{name}] ошибка источника: {exc}", file=sys.stderr)
            failed += 1
            continue
        if args.dry_run:
            print(f"[{name}] получено записей: {len(records)}")
            for record in records[:3]:
                print("   ", json.dumps(record, ensure_ascii=False)[:300])
            continue
        with SessionLocal() as session:
            if args.drop_demo and name == names[0]:
                print(f"Удалено демо-событий: {reset_demo(session)}")
            stats = sync_records(session, records, actor=f"sync:{name}", queue=not args.no_verify)
            if not args.no_verify:  # карточки попадают в выдачу только после проверки, спорные остаются в очереди модерации
                verified = verify_pending(session, links=not args.no_links)
                print(f"[{name}] проверка: опубликовано {verified.published}, в очереди модерации {verified.queued} "
                      f"(битых ссылок {verified.link_dead})")
        totals.update(stats)
        print(
            f"[{name}] получено {len(records)}; создано {stats['created']}, обновлено/склеено {stats['merged']} "
            f"(из них подтверждено другим источником: {stats['cross_source']}), без изменений {stats['unchanged']}, "
            f"пропущено {stats['skipped']}"
        )
    if not args.dry_run and len(names) > 0:
        from .dedup_audit import find_pairs

        with SessionLocal() as session:
            left = find_pairs(session)
        print(f"Итого: создано {totals['created']}, склеено дублей между источниками {totals['cross_source']}; "
              f"похожих пар для проверки: {len(left)} (python -m app.dedup_audit)")
    return 1 if failed and failed == len(names) else 0


if __name__ == "__main__":
    sys.exit(main())
