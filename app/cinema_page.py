"""Страница «Кино» (/cinema): фильмы дня, под каждым кинотеатры и время сеансов. Собирается на сервере из событий категории
«кино» (их приносит источник afisha-cinema: по записи на фильм, кинотеатр и день), параметры в адресной строке, работает
без JavaScript. Время сеансов берётся из текста описания, где адаптер перечисляет все сеансы дня; если разобрать не
удалось, показывается время начала записи. Ничего не домысливается."""
import re
from collections import OrderedDict
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import now_local
from .formatting import MONTHS_GEN, WEEKDAYS, is_free, price_text
from .models import Event
from .pages import _https, _layout, esc
from .search import SearchFilters, _conditions, search

DAYS_AHEAD = 7  # на сколько дней вперёд есть переключатель дней
TILE = "relative rounded-card border border-stroke bg-glass p-[18px] shadow-surface w1000:p-4"
CHIP = ("inline-flex min-h-[40px] flex-none items-center whitespace-nowrap rounded-full border border-stroke bg-glass px-4 text-[.93rem] font-semibold text-ink "
        "no-underline shadow-[0_4px_14px_rgba(52,64,120,.08)] tx-chip hover:bg-glass-hi aria-[current=true]:border-brand aria-[current=true]:bg-brand "
        "aria-[current=true]:text-white")
TIME = ("inline-flex min-h-[36px] items-center rounded-[10px] border border-stroke bg-white/80 px-3 text-[.95rem] font-extrabold "
        "text-brand-ink no-underline [font-variant-numeric:tabular-nums] tx-chip hover:bg-brand-tint")
SESSIONS = re.compile(r"Сеансы \d{2}\.\d{2}: (.+?)\.?\s*$", re.S)
FACTS = re.compile(r"^(?P<facts>.*?Длительность: [^.]*\.)\s*(?P<rest>.*)$", re.S)
STAT_ICON = {
    "film": '<rect x="3" y="3" width="18" height="18" rx="2.5"/><path d="M7 3v18M17 3v18M3 8h4M3 12h4M3 16h4M17 8h4M17 12h4M17 16h4"/>',
    "pin": '<path d="M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0z"/><circle cx="12" cy="10" r="3"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
}
SVG = ('<svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
       'stroke-linejoin="round" aria-hidden="true">{}</svg>')


def _day(query, today: date) -> date:
    try:
        day = date.fromisoformat(query.get("day", ""))
    except ValueError:
        return today
    return day if today <= day <= today + timedelta(days=DAYS_AHEAD) else today


def _split_description(text: str | None) -> tuple[str, str, list[str]]:
    """Описание записи → (факты о фильме, сюжет, время сеансов). Формат задаёт адаптер afisha_cinema."""
    text = (text or "").strip()
    times: list[str] = []
    match = SESSIONS.search(text)
    if match:
        times = [t.strip() for t in match.group(1).split(", ") if t.strip()]
        text = text[:match.start()].strip()
    facts = rest = ""
    parts = FACTS.match(text)
    if parts:
        facts, rest = parts.group("facts").strip(), parts.group("rest").strip()
    else:
        rest = text
    return facts, rest, times


def _film_key(event: Event) -> str:
    return re.sub(r"\s+", " ", event.title.strip().lower())


def group_films(events: list[Event]) -> list[dict]:
    """События дня → фильмы: у каждого список кинотеатров и время сеансов. Порядок: по ближайшему сеансу."""
    films: OrderedDict[str, dict] = OrderedDict()
    for event in events:
        facts, synopsis, times = _split_description(event.description)
        film = films.setdefault(_film_key(event), {"title": event.title, "image": event.image_url, "age": event.age_limit, "facts": facts,
                                                   "synopsis": synopsis, "places": [], "prices": []})
        film["image"] = film["image"] or event.image_url
        film["facts"] = film["facts"] or facts
        film["synopsis"] = film["synopsis"] or synopsis
        if film["age"] is None:
            film["age"] = event.age_limit
        if event.price_min is not None:
            film["prices"].append(event.price_min)
        film["places"].append({"id": event.id, "venue": event.venue or "Кинотеатр", "city": event.city, "address": event.address,
                               "times": times or [f"{event.start_at:%H:%M}"], "start": event.start_at, "price": price_text(event),
                               "free": is_free(event)})
    for film in films.values():
        film["places"].sort(key=lambda p: (p["start"], p["venue"]))
    return list(films.values())


def _cinema_cities(session: Session, now: datetime) -> list[tuple[str, int]]:
    conds = _conditions(SearchFilters(category="cinema"), now)
    return list(session.execute(select(Event.city, func.count()).where(*conds).group_by(Event.city).order_by(func.count().desc())).all())


def _link(day: date, city: str, today: date) -> str:
    params = []
    if day != today:
        params.append(f"day={day.isoformat()}")
    if city:
        params.append("city=" + esc(city))
    return "/cinema" + ("?" + "&amp;".join(params) if params else "")


def _stat(icon: str, tone: str, value: int, label: str) -> str:
    return (f'<div class="{TILE} flex items-center gap-3 py-[14px] w600:flex-col w600:items-start w600:gap-2 w600:p-3"><span class="grid h-[46px] w-[46px] flex-none place-items-center rounded-field '
            f'bg-[var(--tile-bg)] text-[var(--tile-fg)] {tone}">{SVG.format(STAT_ICON[icon])}</span><div class="grid"><strong '
            f'class="text-[1.6rem] font-extrabold leading-none tracking-[-.02em] [font-variant-numeric:tabular-nums]">{value}</strong>'
            f'<span class="text-[.88rem] text-ink-3">{esc(label)}</span></div></div>')


def _film_html(film: dict, show_city: bool) -> str:
    image = _https(film["image"])
    fallback = f'<span class="grid h-full w-full place-items-center bg-[var(--tile-bg)] text-[var(--tile-fg)] tone-cinema">{SVG.format(STAT_ICON["film"])}</span>'
    # Постер лежит поверх иконки; не загрузился (data-hide-on-error: ui.js убирает такую картинку), остаётся иконка кино.
    poster = fallback + (f'<img class="absolute inset-0 h-full w-full object-cover" src="{esc(image)}" alt="Постер фильма «{esc(film["title"])}»" '
                         f'loading="lazy" referrerpolicy="no-referrer" data-hide-on-error>' if image else "")
    age = f'<span class="rounded-full bg-brand-tint px-[10px] py-[3px] text-[.8rem] font-[750] text-brand-ink">{int(film["age"])}+</span>' \
        if film["age"] is not None else ""
    lowest = min(film["prices"]) if film["prices"] else None
    price = (f'<span class="rounded-full border border-stroke bg-white/80 px-[10px] py-[3px] text-[.8rem] font-[750] text-ink-2">от {lowest} ₽</span>'
             if lowest else "")
    places = []
    for place in film["places"]:
        where = place["venue"] + (f", {place['city']}" if show_city and place["city"] else "")
        times = "".join(f'<a class="{TIME}" href="/event/{int(place["id"])}">{esc(t)}</a>' for t in place["times"])
        places.append(f'<li class="grid gap-[6px] border-t border-stroke pt-3 first:border-0 first:pt-0"><a class="font-bold text-ink no-underline '
                      f'hover:underline" href="/event/{int(place["id"])}">{esc(where)}</a><div class="flex flex-wrap gap-2">{times}</div></li>')
    synopsis = (f'<p class="m-0 line-clamp-3 text-[.93rem] text-ink-3">{esc(film["synopsis"])}</p>' if film["synopsis"] else "")
    facts = f'<p class="m-0 text-[.9rem] text-ink-3">{esc(film["facts"])}</p>' if film["facts"] else ""
    return (f'<article class="{TILE} grid grid-cols-[104px_minmax(0,1fr)] gap-4 w600:grid-cols-[84px_minmax(0,1fr)] w600:gap-3">'
            f'<div class="relative aspect-[2/3] overflow-hidden rounded-field border border-stroke bg-glass-soft">{poster}</div>'
            f'<div class="grid min-w-0 content-start gap-2"><h2 class="m-0 text-[1.2rem] font-extrabold leading-[1.25] tracking-[-.015em] '
            f'[overflow-wrap:anywhere]">{esc(film["title"])}</h2><div class="flex flex-wrap items-center gap-2">{age}{price}</div>{facts}{synopsis}'
            f'<ul class="m-0 mt-2 grid list-none gap-3 p-0">{"".join(places)}</ul></div></article>')


def build_page(session: Session, query) -> str:
    now = now_local()
    today = now.date()
    day = _day(query, today)
    city = (query.get("city") or "").strip()[:64]
    cities = _cinema_cities(session, now)
    known = [name for name, _ in cities]
    if city not in known:
        city = ""
    events, _total = search(session, SearchFilters(category="cinema", city=city or None, date_from=day, date_to=day, limit=100), now)
    films = group_films(events)
    cinemas = {(p["venue"], p["city"]) for f in films for p in f["places"]}
    sessions = sum(len(p["times"]) for f in films for p in f["places"])
    day_label = "сегодня" if day == today else "завтра" if day == today + timedelta(days=1) else f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS_GEN[day.month - 1]}"

    day_chips = "".join(
        f'<a class="{CHIP}" href="{_link(today + timedelta(days=i), city, today)}" aria-current="{"true" if today + timedelta(days=i) == day else "false"}">'
        + ("Сегодня" if i == 0 else "Завтра" if i == 1 else f"{WEEKDAYS[(today + timedelta(days=i)).weekday()]}, {(today + timedelta(days=i)).day}") + "</a>"
        for i in range(DAYS_AHEAD + 1))
    city_chips = ""
    if len(known) > 1:
        chips = [f'<a class="{CHIP}" href="{_link(day, "", today)}" aria-current="{"true" if not city else "false"}">Все города</a>']
        chips += [f'<a class="{CHIP}" href="{_link(day, name, today)}" aria-current="{"true" if name == city else "false"}">{esc(name)}</a>' for name in known]
        city_chips = f'<div class="mt-4"><h2 class="mb-2 text-[.76rem] font-extrabold uppercase tracking-[.08em] text-ink-3">Город</h2><div class="flex flex-wrap gap-2">{"".join(chips)}</div></div>'

    head = f"""    <section class="grid grid-cols-3 gap-4 w600:gap-3" aria-labelledby="cinema-title">
      <div class="{TILE} col-span-3 bg-glass-hi">
        <h1 id="cinema-title" class="mb-2 text-[length:clamp(1.8rem,4vw,2.6rem)] font-extrabold leading-[1.06] tracking-[-.03em]">Кино <span class="text-brand">на {esc(day_label)}</span></h1>
        <p class="m-0 max-w-[62ch] text-[1.05rem] text-ink-3">Фильмы, кинотеатры и время сеансов. Данные взяты с Афиши (afisha.ru): цены и время перед походом сверьте по ссылке на источник.</p>
        <div class="mt-4"><h2 class="mb-2 text-[.76rem] font-extrabold uppercase tracking-[.08em] text-ink-3">День</h2><div class="flex flex-wrap gap-2 w600:flex-nowrap w600:overflow-x-auto">{day_chips}</div></div>
        {city_chips}
      </div>
      {_stat("film", "tone-cinema", len(films), "фильмов")}
      {_stat("pin", "tone-excursion", len(cinemas), "кинотеатров")}
      {_stat("clock", "tone-festival", sessions, "сеансов")}
    </section>"""

    if films:
        show_city = not city and len({p["city"] for f in films for p in f["places"]}) > 1
        body = f'    <section class="mt-4 grid grid-cols-2 gap-4 w1000:grid-cols-1" aria-label="Фильмы">{"".join(_film_html(f, show_city) for f in films)}</section>'
    elif not cities:
        body = (f'    <section class="{TILE} mt-4 text-center"><p class="m-0 text-[1.1rem] font-bold">Расписание кино пока не загружено.</p>'
                f'<p class="mx-auto mb-0 mt-2 max-w-[52ch] text-ink-3">Сеансы приходят из источника по расписанию сбора. Загляните позже или откройте <a class="font-semibold text-brand-ink" href="/">афишу других событий</a>.</p></section>')
    else:
        other = next((i for i in range(DAYS_AHEAD + 1) if today + timedelta(days=i) != day and _has(session, today + timedelta(days=i), city, now)), None)
        hint = (f'Есть сеансы на <a class="font-semibold text-brand-ink" href="{_link(today + timedelta(days=other), city, today)}">другой день</a>.'
                if other is not None else ("Попробуйте выбрать «Все города»." if city else "Попробуйте другой день."))
        body = (f'    <section class="{TILE} mt-4 text-center"><p class="m-0 text-[1.1rem] font-bold">На этот день сеансов не нашлось.</p>'
                f'<p class="mx-auto mb-0 mt-2 max-w-[52ch] text-ink-3">{hint}</p></section>')
    return _layout("Кино: расписание сеансов в Башкортостане", "Фильмы и сеансы в кинотеатрах Башкортостана по дням и городам, с ценами и ссылками на источник",
                   head + "\n" + body, path="/cinema", wide=True)


def _has(session: Session, day: date, city: str, now: datetime) -> bool:
    _events, total = search(session, SearchFilters(category="cinema", city=city or None, date_from=day, date_to=day, limit=1), now)
    return total > 0
