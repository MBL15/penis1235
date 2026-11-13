"""Справочник площадок (ТЗ §6.5, §8.2): одна площадка под разными написаниями источников сводится к одному ключу.

Файл data/venues.json (или путь из переменной VENUES_FILE) хранит каноническое название и синонимы. Ключ площадки
(Event.venue_key) строится от канонического названия, поэтому поиск и справочник /venues видят «ГКЗ «Башкортостан»» и
«Государственный концертный зал «Башкортостан»» как одну площадку. Название, пришедшее из источника, в карточке не меняется.
Нет файла или он испорчен: работает прежнее поведение (нормализация регистра, «ё» и знаков препинания)."""
import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from . import config
from .normalize import norm_text

log = logging.getLogger("afisha.venues")
VENUES_FILE = Path(os.getenv("VENUES_FILE") or config.BASE_DIR / "data" / "venues.json")


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict[str, tuple[str, str]]:
    """{нормализованное написание: (каноническое название, ключ)}; mtime в аргументах сбрасывает кеш при правке файла."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = data["venues"] if isinstance(data, dict) else data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("Справочник площадок не прочитан (%s): используется нормализация без синонимов", exc)
        return {}
    known: dict[str, tuple[str, str]] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
            continue
        name = str(entry["name"]).strip()
        key = norm_text(name)
        for spelling in [name, *[str(a) for a in entry.get("aliases", []) if str(a).strip()]]:
            known[norm_text(spelling)] = (name, key)
    return known


def _directory() -> dict[str, tuple[str, str]]:
    path = Path(VENUES_FILE)
    try:
        return _load(str(path), path.stat().st_mtime)
    except OSError:
        return {}


def canonical_venue(name: str | None) -> str | None:
    """Каноническое название площадки из справочника или None, если площадки там нет."""
    found = _directory().get(norm_text(name or ""))
    return found[0] if found else None


def venue_key_for(name: str | None) -> str:
    """Ключ площадки: у известной — от канонического названия (синонимы сводятся в одно), у остальных — нормализованный текст."""
    key = norm_text(name or "")
    found = _directory().get(key)
    return found[1] if found else key


def filter_key(text: str | None) -> str:
    """Ключ для поиска по части названия: точный синоним («ГКЗ») заменяется ключом канонической площадки."""
    return venue_key_for(text)
