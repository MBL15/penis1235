"""Поиск и фильтрация событий. Один код обслуживает REST API, бота и ИИ-агента."""
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from .config import now_local
from .formatting import ONGOING_GRACE
from .models import Event
from .normalize import ACTIVE_STATUSES, canonical_category, canonical_city, norm_text, stem, tokens

WHEN_VALUES = ("today", "tomorrow", "weekend", "week")
MAX_QUERY_WORDS = 8  # каждое слово — отдельный LIKE по всей таблице: длинный запрос не должен её перебирать сотни раз


@dataclass
class SearchFilters:
    q: str | None = None
    city: str | None = None
    category: str | None = None  # slug или несколько через запятую
    date_from: date | None = None
    date_to: date | None = None
    price_max: int | None = None
    free_only: bool = False
    age: int | None = None  # возраст посетителя: показываем события с ограничением не выше
    kids: bool | None = None  # True — только «детские/семейные»
    statuses: list[str] | None = None  # None = только состоявшиеся; ["all"] = любые
    venue: str | None = None  # площадка: часть названия (сравнивается по нормализованному тексту)
    include_drafts: bool = False  # черновики видит только модератор
    publication: str | None = None  # модератору: draft — только черновики, published — только опубликованные
    include_past: bool = False
    sort: str = "date"  # date | price
    limit: int = 20
    offset: int = 0

    def describe(self) -> dict:
        """Фильтры без пустых значений: для ответа API и отладки."""
        out = {k: v for k, v in asdict(self).items() if v not in (None, False, [], "")}
        for key in ("date_from", "date_to"):
            if key in out:
                out[key] = out[key].isoformat()
        for key in ("sort", "limit", "offset"):
            out.pop(key, None)
        return out


def resolve_when(when: str, today: date) -> tuple[date, date]:
    """Быстрые периоды: today | tomorrow | weekend | week → (с, по) включительно."""
    if when == "today":
        return today, today
    if when == "tomorrow":
        return today + timedelta(days=1), today + timedelta(days=1)
    if when == "weekend":
        weekday = today.weekday()  # пн=0 … вс=6
        if weekday == 6:
            return today, today
        saturday = today + timedelta(days=5 - weekday) if weekday < 5 else today
        return saturday, saturday + timedelta(days=1)
    if when == "week":
        return today, today + timedelta(days=6)
    raise ValueError(f"Неизвестный период: {when}")


def _conditions(f: SearchFilters, now: datetime) -> list:
    conds = []
    if f.publication == "draft":
        conds.append(Event.publication == "draft")
    elif f.publication == "published" or not f.include_drafts:
        conds.append(or_(Event.publication.is_(None), Event.publication != "draft"))
    if f.venue:
        key = norm_text(f.venue[:200])
        if key:
            conds.append(Event.venue_key.like(f"%{key}%"))

    if not f.include_past:
        conds.append(
            or_(
                Event.end_at >= now,
                and_(Event.end_at.is_(None), Event.start_at >= now - ONGOING_GRACE),
                Event.series_until >= now,  # серия, пока не кончилась; по дате она ищется только в день известного показа
            )
        )
    if f.date_from is not None:
        conds.append(func.coalesce(Event.end_at, Event.start_at) >= datetime.combine(f.date_from, time.min))
    if f.date_to is not None:
        conds.append(Event.start_at <= datetime.combine(f.date_to, time.max))

    if f.city:
        conds.append(Event.city == (canonical_city(f.city) or f.city))
    if f.category:
        # Одна категория или несколько через запятую: «theatre,concert» — события любой из них.
        slugs = {canonical_category(part) for part in f.category.split(",") if part.strip()}
        if slugs:
            conds.append(Event.category.in_(sorted(slugs)))

    statuses = f.statuses or list(ACTIVE_STATUSES)
    if "all" not in statuses:
        conds.append(Event.status.in_(statuses))

    if f.free_only:
        conds.append(and_(Event.price_min == 0, or_(Event.price_max.is_(None), Event.price_max == 0)))
    elif f.price_max is not None:
        conds.append(and_(Event.price_min.is_not(None), Event.price_min <= f.price_max))

    if f.age is not None:
        conds.append(Event.age_limit <= f.age)
    if f.kids:
        conds.append(Event.kids_friendly.is_(True))

    if f.q:
        for word in tokens(f.q[:200])[:MAX_QUERY_WORDS]:
            conds.append(Event.search_text.like(f"%{stem(word)}%"))
    return conds


def search(session: Session, f: SearchFilters, now: datetime | None = None) -> tuple[list[Event], int]:
    now = now or now_local()
    conds = _conditions(f, now)

    total = session.scalar(select(func.count()).select_from(Event).where(*conds)) or 0

    stmt = select(Event).where(*conds)
    if f.sort == "price":
        stmt = stmt.order_by(func.coalesce(Event.price_min, 10**9), Event.start_at, Event.id)
    else:
        # Уже идущие многодневные события (выставки) — после разовых, иначе они забивают начало афиши.
        ongoing = case(
            (and_(Event.start_at < now, or_(Event.end_at.is_not(None), Event.series_until.is_not(None))), 1), else_=0
        )
        stmt = stmt.order_by(ongoing, Event.start_at, Event.id)
    stmt = stmt.limit(max(1, min(f.limit, 100))).offset(max(0, f.offset))
    return list(session.scalars(stmt)), total


def get_event(session: Session, event_id: int, *, include_drafts: bool = False) -> Event | None:
    """Событие по номеру. Черновик отдаётся только с include_drafts (модератору): публике он не существует."""
    event = session.get(Event, event_id)
    if event is not None and event.publication == "draft" and not include_drafts:
        return None
    return event


def venues(session: Session, now: datetime | None = None, *, city: str | None = None, q: str | None = None,
           limit: int = 200) -> list[dict]:
    """Справочник площадок с числом актуальных событий. Написания, различающиеся регистром, «ё» и знаками препинания
    («Дворец молодёжи» и «Дворец Молодежи»), считаются одной площадкой; полный справочник с синонимами в планах."""
    conds = _conditions(SearchFilters(city=city), now or now_local())
    conds += [Event.venue_key.is_not(None), Event.venue_key != ""]
    rows = session.execute(
        select(Event.venue_key, Event.venue, Event.city, func.count())
        .where(*conds).group_by(Event.venue_key, Event.venue, Event.city)
    ).all()
    wanted = norm_text(q[:200]) if q else ""
    groups: dict[str, dict] = {}
    for key, name, city_name, count in rows:
        if wanted and wanted not in key:
            continue
        group = groups.setdefault(key, {"names": {}, "cities": {}, "count": 0})
        group["names"][name] = group["names"].get(name, 0) + count
        group["cities"][city_name] = group["cities"].get(city_name, 0) + count
        group["count"] += count
    result = []
    for group in groups.values():
        names = sorted(group["names"], key=lambda n: (-group["names"][n], n))
        result.append({"name": names[0], "city": max(group["cities"], key=group["cities"].get), "count": group["count"],
                       "names": names[1:6]})
    result.sort(key=lambda v: (-v["count"], v["name"]))
    return result[: max(1, min(limit, 500))]


def facets(session: Session, now: datetime | None = None) -> dict:
    """Города и категории с числом актуальных событий: для фильтров в интерфейсе, боте и промпте агента."""
    conds = _conditions(SearchFilters(), now or now_local())
    cities = session.execute(
        select(Event.city, func.count()).where(*conds).group_by(Event.city).order_by(func.count().desc())
    ).all()
    categories = session.execute(
        select(Event.category, func.count()).where(*conds).group_by(Event.category).order_by(func.count().desc())
    ).all()
    return {
        "cities": [{"name": name, "count": count} for name, count in cities],
        "categories": [{"slug": slug, "count": count} for slug, count in categories],
    }
