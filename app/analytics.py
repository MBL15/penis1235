"""Аналитика: профиль интересов пользователя с рекомендациями и сводка по всем пользователям для администратора (CSV).

Откуда берутся интересы человека (только его собственные данные, ТЗ §10):
  * история посещений — открытые карточки (вес 1) и переходы к билетам (вес 3); пишется только с согласия;
  * подписки на город и категорию (вес 3) — их человек оформил сам;
  * сохранённые поиски (вес 2) — город, категория, «бесплатно», «с детьми».
Рекомендации — ближайшие опубликованные события, которых человек ещё не открывал, по сумме совпадений с профилем; у каждой
объяснение одной-двумя причинами. Нет ни истории, ни подписок — показываем популярное у посетителей за 30 дней.
Сводка администратора: число и активность аккаунтов, вовлечённость, популярные события и категории, подписки, обращения,
ответы помощника; выгрузка — по строке на аккаунт, без номера Telegram (персональные данные минимизированы)."""
import csv
import io
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .models import AgentTrace, Event, Feedback, SavedSearch, Subscription, User, VisitHistory
from .normalize import CATEGORIES
from .search import SearchFilters, _conditions

WEIGHT_VIEW, WEIGHT_TICKET, WEIGHT_SUB, WEIGHT_SEARCH = 1, 3, 3, 2
PROFILE_DAYS = 180  # история старше в профиль не идёт
FEEDBACK_LABELS = {"new": "новые", "in_progress": "в работе", "processed": "обработаны"}
MAX_PER_CATEGORY = 3  # в подборке не больше трёх событий одной категории: рекомендации не должны быть однообразными


@dataclass
class Profile:
    categories: Counter = field(default_factory=Counter)
    cities: Counter = field(default_factory=Counter)
    venues: Counter = field(default_factory=Counter)
    prices: list[int] = field(default_factory=list)
    kids_views: int = 0
    free_interest: bool = False
    views: int = 0
    tickets: int = 0
    seen: set[int] = field(default_factory=set)
    first_visit: datetime | None = None
    last_visit: datetime | None = None
    subscriptions: int = 0
    searches: int = 0

    @property
    def empty(self) -> bool:
        return not (self.categories or self.cities or self.venues)

    @property
    def median_price(self) -> int | None:
        return int(statistics.median(self.prices)) if self.prices else None


def _values(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(v) for v in raw if v]
    return [v.strip() for v in str(raw or "").split(",") if v.strip()]


def build_profile(session: Session, user: User, now: datetime) -> Profile:
    p = Profile()
    since = now - timedelta(days=PROFILE_DAYS)
    rows = session.execute(
        select(VisitHistory.kind, VisitHistory.viewed_at, Event)
        .join(Event, Event.id == VisitHistory.event_id)
        .where(VisitHistory.user_id == user.id, VisitHistory.viewed_at >= since)
    ).all()
    for kind, at, event in rows:
        weight = WEIGHT_TICKET if kind == "ticket" else WEIGHT_VIEW
        p.tickets += kind == "ticket"
        p.views += kind != "ticket"
        p.seen.add(event.id)
        p.categories[event.category] += weight
        if event.city:
            p.cities[event.city] += weight
        if event.venue_key:
            p.venues[event.venue_key] += weight
        if event.price_min is not None:
            p.prices.append(event.price_min)
        p.kids_views += bool(event.kids_friendly)
        p.first_visit = min(filter(None, [p.first_visit, at]))
        p.last_visit = max(filter(None, [p.last_visit, at]))
    subs = session.scalars(select(Subscription).where(or_(Subscription.user_id == user.id, Subscription.chat_id == user.telegram_id))).all()
    p.subscriptions = len(subs)
    for sub in subs:
        if sub.category:
            p.categories[sub.category] += WEIGHT_SUB
        if sub.city:
            p.cities[sub.city] += WEIGHT_SUB
    searches = session.scalars(select(SavedSearch).where(SavedSearch.user_id == user.id)).all()
    p.searches = len(searches)
    for search in searches:
        f = search.filters or {}
        for slug in _values(f.get("category")) + _values(f.get("categories")):
            p.categories[slug] += WEIGHT_SEARCH
        for city in _values(f.get("city")):
            p.cities[city] += WEIGHT_SEARCH
        p.free_interest = p.free_interest or bool(f.get("free"))
        if f.get("kids"):
            p.kids_views += WEIGHT_SEARCH
    return p


def _label(slug: str) -> str:
    return CATEGORIES.get(slug, slug)


def _upcoming(session: Session, now: datetime, limit: int = 400) -> list[Event]:
    return list(session.scalars(select(Event).where(*_conditions(SearchFilters(), now)).order_by(Event.start_at).limit(limit)))


def popular_event_ids(session: Session, now: datetime, days: int = 30) -> Counter:
    rows = session.execute(select(VisitHistory.event_id, func.count()).where(VisitHistory.viewed_at >= now - timedelta(days=days))
                           .group_by(VisitHistory.event_id)).all()
    return Counter(dict(rows))


def recommend(session: Session, user: User, now: datetime, limit: int = 6, profile: Profile | None = None) -> list[tuple[Event, list[str]]]:
    """(событие, причины) — лучшие первыми. Причины — короткие фразы для подписи под карточкой."""
    p = profile or build_profile(session, user, now)
    candidates = [e for e in _upcoming(session, now) if e.id not in p.seen]
    popular = popular_event_ids(session, now)
    if p.empty:
        ranked = sorted(candidates, key=lambda e: (-popular.get(e.id, 0), -len(e.sources or []), e.start_at))
        return [(e, ["популярно у посетителей афиши" if popular.get(e.id) else "ближайшее событие из нескольких источников"
                     if len(e.sources or []) > 1 else "ближайшее событие в афише"]) for e in ranked[:limit]]
    cat_total, city_total = sum(p.categories.values()) or 1, sum(p.cities.values()) or 1
    top_city = p.cities.most_common(1)[0][0] if p.cities else None
    kids = p.kids_views >= max(2, (p.views + p.tickets) // 2)
    median = p.median_price
    scored = []
    for e in candidates:
        reasons: list[tuple[float, str]] = []
        score = 0.0
        if p.categories.get(e.category):
            part = 4 * p.categories[e.category] / cat_total
            score += part
            reasons.append((part, f"вы часто выбираете «{_label(e.category)}»"))
        if e.city and p.cities.get(e.city):
            part = 2 * p.cities[e.city] / city_total
            score += part
            reasons.append((part, f"в городе {e.city}, где вы ищете события"))
        elif top_city and e.city != top_city:
            score -= 1  # другой город: показываем, только если в своём мало подходящего
        if e.venue_key and p.venues.get(e.venue_key):
            score += 1
            reasons.append((1, f"на площадке «{e.venue}», где вы уже бывали"))
        if median is not None and e.price_min is not None and e.price_min <= median * 1.3:
            score += .6
            reasons.append((.6, "по цене как вы обычно выбираете" if e.price_min else "вход свободный"))
        if p.free_interest and e.price_min == 0:
            score += 1
            reasons.append((1, "бесплатно — вы сохраняли такой поиск"))
        if kids and e.kids_friendly:
            score += .8
            reasons.append((.8, "подходит для похода с детьми"))
        score += min(popular.get(e.id, 0), 5) * .1 + (.3 if (e.start_at - now) <= timedelta(days=7) else 0)
        if score > 0 and reasons:
            scored.append((score, e, [text for _, text in sorted(reasons, key=lambda r: -r[0])[:2]]))
    scored.sort(key=lambda item: (-item[0], item[1].start_at))
    picked, per_cat = [], Counter()
    for _, e, why in scored:
        if per_cat[e.category] >= MAX_PER_CATEGORY:
            continue
        per_cat[e.category] += 1
        picked.append((e, why))
        if len(picked) == limit:
            break
    return picked


def profile_out(p: Profile) -> dict:
    return {
        "views": p.views, "ticket_clicks": p.tickets, "events_viewed": len(p.seen),
        "top_categories": [{"slug": s, "label": _label(s), "weight": w} for s, w in p.categories.most_common(3)],
        "top_cities": [{"name": c, "weight": w} for c, w in p.cities.most_common(3)],
        "median_price": p.median_price, "subscriptions": p.subscriptions, "saved_searches": p.searches,
        "first_visit": p.first_visit, "last_visit": p.last_visit,
    }


# ---------- сводка по всем пользователям ----------

def overview(session: Session, now: datetime) -> dict:
    d7, d30 = now - timedelta(days=7), now - timedelta(days=30)
    count = lambda stmt: session.scalar(stmt) or 0  # noqa: E731
    users_total = count(select(func.count()).select_from(User))
    from .accounts import ROLES

    roles = {ROLES.get(r, r): n for r, n in session.execute(select(User.role, func.count()).group_by(User.role)).all()}
    visits = session.execute(select(VisitHistory.kind, VisitHistory.user_id, Event.id, Event.title, Event.category, Event.city)
                             .join(Event, Event.id == VisitHistory.event_id).where(VisitHistory.viewed_at >= d30)).all()
    by_event, by_cat, by_city, daily = Counter(), Counter(), Counter(), Counter()
    titles = {}
    for kind, _uid, eid, title, cat, city in visits:
        by_event[eid] += 1
        titles[eid] = title
        by_cat[cat] += 1
        if city:
            by_city[city] += 1
    for (at,) in session.execute(select(VisitHistory.viewed_at).where(VisitHistory.viewed_at >= now - timedelta(days=14))).all():
        daily[at.date().isoformat()] += 1
    subs = session.execute(select(Subscription.city, Subscription.category)).all()
    traces = dict(session.execute(select(AgentTrace.mode, func.count()).where(AgentTrace.created_at >= d7).group_by(AgentTrace.mode)).all())
    feedback = {FEEDBACK_LABELS.get(st, st): n for st, n in session.execute(select(Feedback.status, func.count()).group_by(Feedback.status)).all()}
    return {
        "generated_at": now,
        "users": {
            "total": users_total, "by_role": roles,
            "new_7d": count(select(func.count()).select_from(User).where(User.created_at >= d7)),
            "new_30d": count(select(func.count()).select_from(User).where(User.created_at >= d30)),
            "active_7d": count(select(func.count()).select_from(User).where(User.last_seen_at >= d7)),
            "active_30d": count(select(func.count()).select_from(User).where(User.last_seen_at >= d30)),
            "history_enabled": count(select(func.count()).select_from(User).where(User.history_enabled.is_(True))),
        },
        "engagement_30d": {
            "visits": sum(1 for v in visits if v[0] != "ticket"), "ticket_clicks": sum(1 for v in visits if v[0] == "ticket"),
            "users_with_visits": len({v[1] for v in visits}),
            "top_events": [{"id": eid, "title": titles[eid], "visits": n} for eid, n in by_event.most_common(10)],
            "top_categories": [{"slug": s, "label": _label(s), "visits": n} for s, n in by_cat.most_common()],
            "top_cities": [{"name": c, "visits": n} for c, n in by_city.most_common(10)],
            "daily_visits_14d": [{"date": (now - timedelta(days=i)).date().isoformat(),
                                  "visits": daily.get((now - timedelta(days=i)).date().isoformat(), 0)} for i in range(13, -1, -1)],
        },
        "subscriptions": {
            "total": len(subs),
            "by_city": [{"name": c or "вся республика", "count": n} for c, n in Counter(c for c, _ in subs).most_common(10)],
            "by_category": [{"label": _label(s) if s else "любая", "count": n} for s, n in Counter(s for _, s in subs).most_common(10)],
        },
        "saved_searches": count(select(func.count()).select_from(SavedSearch)),
        "feedback": feedback,
        "assistant_7d": traces,
    }


CSV_COLUMNS = ["user_id", "display_name", "role", "created_at", "last_seen_at", "history_enabled", "visits", "ticket_clicks",
               "events_viewed", "subscriptions", "saved_searches", "feedback", "top_category", "top_city", "median_price"]
CSV_HEADERS = ["№ аккаунта", "Имя", "Роль", "Создан", "Последний визит", "История включена", "Просмотры", "Переходы к билетам",
               "Разных событий", "Подписки", "Сохранённые поиски", "Обращения", "Любимая категория", "Город", "Медианная цена, ₽"]


def users_rows(session: Session, now: datetime) -> list[dict]:
    from .accounts import ROLES

    feedback = Counter(dict(session.execute(select(Feedback.user_id, func.count()).where(Feedback.user_id.is_not(None))
                                            .group_by(Feedback.user_id)).all()))
    rows = []
    for user in session.scalars(select(User).order_by(User.id)):
        p = build_profile(session, user, now)
        top_cat = p.categories.most_common(1)
        top_city = p.cities.most_common(1)
        rows.append({
            "user_id": user.id, "display_name": user.display_name, "role": ROLES.get(user.role, user.role),
            "created_at": user.created_at.strftime("%Y-%m-%d %H:%M") if user.created_at else "",
            "last_seen_at": user.last_seen_at.strftime("%Y-%m-%d %H:%M") if user.last_seen_at else "",
            "history_enabled": "да" if user.history_enabled else "нет", "visits": p.views, "ticket_clicks": p.tickets,
            "events_viewed": len(p.seen), "subscriptions": p.subscriptions, "saved_searches": p.searches,
            "feedback": feedback.get(user.id, 0), "top_category": _label(top_cat[0][0]) if top_cat else "",
            "top_city": top_city[0][0] if top_city else "", "median_price": p.median_price if p.median_price is not None else "",
        })
    return rows


def to_csv(rows: list[dict]) -> str:
    """CSV для Excel на русском: разделитель «;», UTF-8 с BOM, первая строка — русские заголовки. Ячейки, похожие на формулу
    (=, +, -, @ в начале имени), экранируются апострофом: имя из Telegram не должно исполниться формулой в таблице."""
    out = io.StringIO()
    out.write("﻿")
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_HEADERS)
    for row in rows:
        values = []
        for col in CSV_COLUMNS:
            value = row.get(col, "")
            if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
                value = "'" + value
            values.append(value)
        writer.writerow(values)
    return out.getvalue()
