"""Дедупликация: одно и то же событие из разных источников склеивается в одну запись."""
import hashlib
from datetime import datetime, time, timedelta
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Event
from .normalize import STATUS_SEVERITY, norm_text, title_key
from .venues import venue_key_for

SIMILARITY_THRESHOLD = 0.82
STRICT_THRESHOLD = 0.95  # когда площадки явно разные, нужно почти полное совпадение названий
MAX_TIME_GAP = timedelta(hours=3)  # дневной и вечерний сеанс одного спектакля — разные события


def make_dedup_key(city: str, start_at: datetime, title: str) -> str:
    raw = f"{city.lower()}|{start_at:%Y-%m-%d}|{title_key(title)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def similarity(a: str, b: str) -> float:
    """0..1: похожесть двух нормализованных заголовков (порядок и вложенность слов учитываются)."""
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    numbers_a, numbers_b = {t for t in ta if t.isdigit()}, {t for t in tb if t.isdigit()}
    if numbers_a and numbers_b and numbers_a != numbers_b:
        return 0.0  # «Лекция 1» и «Лекция 2» — разные события, хоть и почти одинаковые строки
    score = SequenceMatcher(None, a, b).ratio()
    shortest = min(len(ta), len(tb))
    if shortest:
        containment = len(ta & tb) / shortest
        # «Лебединое озеро» ⊂ «Лебединое озеро. П. И. Чайковский»
        score = max(score, containment * (0.95 if shortest >= 2 else 0.9))
    return score


def _has_time(value: datetime) -> bool:
    return (value.hour, value.minute) != (0, 0)


def _time_close(a: datetime, b: datetime) -> bool:
    if not (_has_time(a) and _has_time(b)):
        return True
    return abs(a - b) <= MAX_TIME_GAP


def _venues_conflict(a: str, b: str) -> bool:
    na, nb = norm_text(a), norm_text(b)
    if not na or not nb:
        return False
    return SequenceMatcher(None, na, nb).ratio() < 0.4 and not (set(na.split()) & set(nb.split()))


def _same_cinema_slot(candidate: Event, venue: str) -> bool:
    """Кино: один фильм одновременно идёт в разных кинотеатрах — это разные события; склеиваются только записи одного кинотеатра."""
    if candidate.category != "cinema":
        return False
    return bool(venue) and (candidate.venue_key or venue_key_for(candidate.venue)) == venue_key_for(venue)


def find_duplicate(
    session: Session, *, city: str, start_at: datetime, title: str, venue: str = "", url: str = "", category: str = ""
) -> Event | None:
    if url:
        same_url = session.scalar(select(Event).where(Event.url == url))
        if same_url is not None:
            return same_url

    day_start = datetime.combine(start_at.date(), time.min)
    candidates = session.scalars(
        select(Event).where(
            Event.city == city,
            Event.start_at >= day_start,
            Event.start_at < day_start + timedelta(days=1),
        )
    ).all()

    key = title_key(title)
    best: tuple[float, Event] | None = None
    for candidate in candidates:
        if category == "cinema" and not _same_cinema_slot(candidate, venue):
            continue
        if not _time_close(candidate.start_at, start_at):
            continue
        score = similarity(key, title_key(candidate.title))
        threshold = STRICT_THRESHOLD if _venues_conflict(candidate.venue, venue) else SIMILARITY_THRESHOLD
        if score >= threshold and (best is None or score > best[0]):
            best = (score, candidate)
    return best[1] if best else None


def worse_status(a: str, b: str) -> str:
    """При расхождении источников верим более «тревожному» статусу: отмена важнее, чем «состоится»."""
    return a if STATUS_SEVERITY.get(a, 0) >= STATUS_SEVERITY.get(b, 0) else b
