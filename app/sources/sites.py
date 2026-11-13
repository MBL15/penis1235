"""Любые сайты с событиями в формате schema.org (театры, филармонии, музеи, площадки, местные афиши).

Сайты описываются в файле data/sources.json (или по пути из переменной SOURCES_FILE), код менять не нужно:

    [
      {"name": "example-theatre.ru", "host": "example-theatre.ru", "city": "Уфа",
       "urls": ["https://example-theatre.ru/afisha/"],
       "follow": "^/afisha/[a-z0-9-]+/$", "max_links": 40, "tz_mode": "honor"}
    ]

name: имя источника в карточке; host: допустимый домен; urls: страницы с афишей; follow (необязательно): регулярное
выражение пути ссылок, по которым нужно перейти со страницы афиши на страницы событий; tz_mode: «honor» (смещение в
разметке верное) или «local» (сайт пишет местное время с ложной меткой UTC, как kassir.ru).
Все правила Fetcher действуют и здесь: robots.txt, паузы, только HTTPS, остановка на капче и блокировке.
"""
import json
import logging
import os
import re
import urllib.parse
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from ..config import BASE_DIR
from .base import BlockedError, Fetcher, SourceError
from .jsonld import extract_events, record_from_ld

log = logging.getLogger("afisha.sites")

SOURCES_FILE = Path(os.getenv("SOURCES_FILE") or BASE_DIR / "data" / "sources.json")
_HREF = re.compile(r'href=["\']([^"\'#]+)["\']', re.I)


def load_sites(path: Path = SOURCES_FILE) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except ValueError as exc:
        raise SourceError(f"{path}: не JSON ({exc})") from exc
    if not isinstance(data, list):
        raise SourceError(f"{path}: ожидается список сайтов")
    sites = []
    for entry in data:
        if not isinstance(entry, dict) or not all(entry.get(k) for k in ("name", "host", "urls")):
            raise SourceError(f"{path}: у сайта должны быть name, host и urls: {entry!r}")
        sites.append(entry)
    return sites


def links_to_follow(html: str, base_url: str, pattern: str, host: str, limit: int) -> list[str]:
    regex = re.compile(pattern)
    found: dict[str, None] = {}
    for href in _HREF.findall(html):
        absolute = urllib.parse.urljoin(base_url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme == "https" and (parts.hostname or "").endswith(host) and regex.search(parts.path):
            found[absolute] = None
        if len(found) >= limit:
            break
    return list(found)


def fetch_site(site: dict, *, fetcher: Fetcher | None = None, today: date | None = None) -> Iterator[dict]:
    fetcher = fetcher or Fetcher((site["host"],), delay=float(site.get("delay", 1.5)))
    mode = site.get("tz_mode", "honor")
    for url in site["urls"]:
        try:
            html = fetcher.get(url)
        except BlockedError:
            raise
        except SourceError as exc:
            log.warning("%s: %s", site["name"], exc)
            continue
        pages = [(url, html)]
        if site.get("follow"):
            for link in links_to_follow(html, url, site["follow"], site["host"], int(site.get("max_links", 40))):
                try:
                    pages.append((link, fetcher.get(link)))
                except BlockedError:
                    raise
                except SourceError as exc:
                    log.warning("%s: %s", site["name"], exc)
        found = 0
        for page_url, page in pages:
            for node in extract_events(page):
                record = record_from_ld(node, source=site["name"], default_city=site.get("city"), tz_mode=mode, page_url=page_url, today=today)
                if record:
                    found += 1
                    yield record
        if not found:
            log.warning("%s: на %s нет событий в формате schema.org", site["name"], url)


def fetch_records(path: Path = SOURCES_FILE, **kwargs) -> Iterator[dict]:
    for site in load_sites(path):
        yield from fetch_site(site, **kwargs)
