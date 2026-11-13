"""Адаптер Kassir.ru (билеты на концерты, театр, шоу, детские, спорт): ufa.kassir.ru и поддомены других городов.

Страницы разделов отдают события в schema.org (<script id="schema-org-graph">, до ~90 событий на раздел), нужные поля
там есть: название, площадка и адрес, цена, ссылка, картинка. Особенность сайта: время начала в разметке записано
с ложной меткой UTC («13:00+00:00», а на странице «начало в 13:00»), поэтому смещение игнорируется (tz_mode='local').
Кроме сайта-источника других запросов нет; robots.txt и паузы соблюдаются, капчу и блокировку не обходим.
"""
import logging
from collections.abc import Iterator
from datetime import date

from .base import BlockedError, Fetcher, SourceError
from .jsonld import extract_events, record_from_ld

log = logging.getLogger("afisha.kassir")

SOURCE_NAME = "kassir.ru"
HOSTS = ("kassir.ru",)

# Поддомены городов Башкортостана (слаг → название). Нет поддомена или он не отвечает: город пропускается с предупреждением.
CITIES = {"ufa": "Уфа", "sterlitamak": "Стерлитамак", "neftekamsk": "Нефтекамск", "salavat": "Салават",
          "sibay": "Сибай", "oktyabrskiy": "Октябрьский"}
# Разделы афиши (без кино: расписание фильмов не относится к культурным событиям республики).
SECTIONS = ("", "bilety-na-koncert", "bilety-v-teatr", "bilety-na-shou", "bilety-na-shou/stendap", "bilety-na-festival",
            "bilety-v-muzei", "detskaya-afisha", "bilety-na-sportivnye-meropriyatiya")
# Первая часть пути страницы события → категория
PATH_CATEGORY = {"koncert": "concert", "teatr": "theatre", "detyam": "kids", "festival": "festival", "muzei": "exhibition",
                 "sport": "sport", "sportivnye-meropriyatiya": "sport", "ekskursii": "excursion"}


def parse_section(html: str, *, city: str, section_url: str, today: date | None = None) -> list[dict]:
    """Страница раздела → записи. Время «как написано» (смещение игнорируется), категория по пути ссылки на событие."""
    records = []
    for node in extract_events(html):
        path = str(node.get("url") or section_url or "").split("/")[3:4]
        record = record_from_ld(
            node, source=SOURCE_NAME, default_city=city, tz_mode="local", page_url=section_url, today=today,
            default_category=PATH_CATEGORY.get(path[0] if path else ""),
            period_as_series=True,  # Kassir.ru записывает серию спектаклей или концертов как период «первая дата – последняя»
        )
        if record is None:
            continue
        records.append(record)
    return records


def fetch_records(
    *,
    cities: dict[str, str] | None = None,
    sections: tuple[str, ...] = SECTIONS,
    fetcher: Fetcher | None = None,
    today: date | None = None,
) -> Iterator[dict]:
    cities = cities or {"ufa": CITIES["ufa"]}
    fetcher = fetcher or Fetcher(HOSTS, delay=1.5)
    for slug, city in cities.items():
        for section in sections:
            url = f"https://{slug}.kassir.ru/{section}".rstrip("/") + ("/" if not section else "")
            try:
                html = fetcher.get(url)
            except BlockedError:
                raise
            except SourceError as exc:
                log.warning("Kassir.ru: %s", exc)
                if not section:  # главная города не открылась: остальные страницы поддомена тоже не откроются
                    break
                continue
            yield from parse_section(html, city=city, section_url=url, today=today)
