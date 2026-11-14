"""Страница «Выходной день» (/plan): форма и результат планировщика. Всё на сервере: параметры в адресной строке, результат
открывается по ссылке и работает без JavaScript."""
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from .config import now_local
from .normalize import canonical_city
from .pages import PRIMARY, _layout, esc
from .planner import PlanRequest, default_day, plan_weekend
from .search import facets

FIELD = ("min-h-[46px] w-full rounded-field border border-line bg-glass-field px-3 text-[1rem] font-medium text-ink tx-field "
         "hover:bg-white/90 focus-visible:border-brand focus-visible:shadow-focus focus-visible:outline-none placeholder:text-ink-4 placeholder:opacity-100")
LABEL = "grid gap-[6px] text-[.82rem] font-bold tracking-[.02em] text-ink-3"
# Плитка бенто: то же матовое стекло, что на главной. TILE: просто плитка, TILE_LIFT: плитка-ссылка с подъёмом при наведении.
TILE = "relative rounded-card border border-stroke bg-glass p-[18px] shadow-surface w1000:p-4"
TILE_LIFT = f"{TILE} tx-glass hover:-translate-y-1 hover:bg-glass-hi hover:shadow-surface-2"
CHIP_ICON = "grid h-[46px] w-[46px] flex-none place-items-center rounded-field bg-[var(--tile-bg)] text-[var(--tile-fg)]"
HEADING = "text-[.76rem] font-extrabold uppercase tracking-[.08em] text-ink-3"

_SVG = ('<svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>')
ICONS = {
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2.5"/><path d="M16 3v4M8 3v4M3 10h18"/>',
    "pin": '<path d="M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0z"/><circle cx="12" cy="10" r="3"/>',
    "kids": '<circle cx="12" cy="12" r="9"/><path d="M8.2 14.2s1.4 2 3.8 2 3.8-2 3.8-2"/><path d="M9 9.5h.01M15 9.5h.01"/>',
    "gift": '<rect x="3" y="8" width="18" height="4" rx="1.2"/><path d="M12 8v13M5 12v8a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-12"/><path d="M7.5 8a2.5 2.5 0 1 1 0-5C10 3 12 8 12 8s2-5 4.5-5a2.5 2.5 0 1 1 0 5"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2.5 12h2M19.5 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "route": '<circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="6" r="2.5"/><path d="M8.5 18H15a3 3 0 0 0 0-6H9a3 3 0 0 1 0-6h6.5"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "list": '<rect x="4" y="4" width="7" height="7" rx="1.8"/><rect x="13" y="4" width="7" height="7" rx="1.8"/><rect x="4" y="13" width="7" height="7" rx="1.8"/><rect x="13" y="13" width="7" height="7" rx="1.8"/>',
}


def _icon(name: str, tone: str) -> str:
    return f'<span class="{CHIP_ICON} {tone}">{_SVG.format(ICONS[name])}</span>'


class FormError(ValueError):
    """Ошибка в значениях формы: текст показывается человеку как есть."""


def _field(label: str, name: str, value: str, kind: str = "text", extra: str = "") -> str:
    return (f'<label class="{LABEL}"><span>{esc(label)}</span>'
            f'<input class="{FIELD}" type="{kind}" name="{name}" value="{esc(value)}" {extra}></label>')


def default_values(now: datetime, query) -> dict:
    saturday = default_day(now)  # тот же день по умолчанию, что у команды /plan в боте
    get = query.get
    return {"day": get("day", saturday.isoformat()), "window_from": get("window_from", "10:00"), "window_to": get("window_to", "22:00"),
            "budget": get("budget", ""), "adults": get("adults", "1"), "children": get("children", ""), "city": get("city", ""),
            "max_travel_min": get("max_travel_min", "40"), "events": get("events", "3")}


def parse_request(values: dict, cities: list[str], today: date) -> PlanRequest:
    """Значения формы → запрос планировщика; любая ошибка — FormError с понятным текстом."""
    try:
        day = date.fromisoformat(values["day"])
    except ValueError:
        raise FormError("Укажите день в формате ГГГГ-ММ-ДД.") from None
    if not today <= day <= today + timedelta(days=90):
        raise FormError("Планировать можно на день от сегодняшнего до 90 дней вперёд.")
    try:
        window_from, window_to = (time(*map(int, values[k].split(":"))) for k in ("window_from", "window_to"))
    except (ValueError, TypeError):
        raise FormError("Время указывайте как ЧЧ:ММ, например 10:00.") from None
    if window_from >= window_to:
        raise FormError("Окно времени: «с» должно быть раньше «по».")
    try:
        children = [int(a) for a in values["children"].replace(";", ",").split(",") if a.strip()]
        budget = int(values["budget"]) if values["budget"].strip() else None
        adults, events, travel = int(values["adults"]), int(values["events"]), int(values["max_travel_min"])
    except ValueError:
        raise FormError("Бюджет, число людей, возраст детей и дорога должны быть числами.") from None
    if not (1 <= adults <= 10 and 2 <= events <= 4 and 0 <= travel <= 240 and len(children) <= 8
            and all(0 <= a <= 17 for a in children) and (budget is None or 0 <= budget <= 1_000_000)):
        raise FormError("Проверьте числа: взрослых от 1 до 10, детей до 8 (возраст 0–17), событий 2–4, дорога до 240 минут.")
    city = canonical_city(values["city"]) if values["city"] else None
    if city and city not in cities:
        raise FormError("Такого города нет в афише.")
    return PlanRequest(day=day, window_from=window_from, window_to=window_to, budget=budget, adults=adults, children=children,
                       city=city, max_travel_min=travel, events=events)


def build_page(session: Session, query) -> str:
    now = now_local()
    today = now.date()
    values = default_values(now, query)
    cities = [c["name"] for c in facets(session)["cities"]]
    if "day" not in query:  # первое открытие: только форма
        return render(values, cities)
    try:
        request = parse_request(values, cities, today)
    except FormError as exc:
        return render(values, cities, error=str(exc))
    return render(values, cities, plan_weekend(session, request, now))


def _tile_head(icon: str, tone: str, title: str, hint: str = "") -> str:
    hint_html = f'<span class="text-[.88rem] text-ink-3">{esc(hint)}</span>' if hint else ""
    return (f'<div class="mb-4 flex items-center gap-3">{_icon(icon, tone)}'
            f'<div class="grid"><strong class="text-[1.05rem] font-extrabold leading-tight">{esc(title)}</strong>{hint_html}</div></div>')


def _count_radios(current: str) -> str:
    items = []
    for n in (2, 3, 4):
        checked = " checked" if str(n) == str(current) else ""
        items.append(
            f'<label class="cursor-pointer"><input class="peer sr-only" type="radio" name="events" value="{n}"{checked}>'
            f'<span class="grid min-h-[56px] place-items-center rounded-field border border-line bg-glass-field text-[1.4rem] font-extrabold tx-chip '
            f'hover:bg-white peer-checked:border-transparent peer-checked:bg-brand peer-checked:text-white peer-checked:shadow-cta '
            f'peer-focus-visible:outline peer-focus-visible:outline-[3px] peer-focus-visible:outline-offset-2 peer-focus-visible:outline-[rgba(79,70,229,.55)]">{n}</span></label>')
    return f'<div class="grid grid-cols-3 gap-2">{"".join(items)}</div>'


HINTS = (  # три короткие плитки над формой: что получится на выходе
    ("check", "tone-kids", "Без пересечений", "по времени"),
    ("route", "tone-excursion", "С дорогой", "оценим минуты в пути"),
    ("gift", "tone-festival", "С ценой", "посчитаем на всех"),
)


def render(values: dict, cities: list[str], result=None, error: str = "") -> str:
    options = "".join(f'<option value="{esc(c)}"{" selected" if c == values.get("city") else ""}>{esc(c)}</option>' for c in cities)
    city_options = f'<option value="">Любой город</option>{options}'
    hints = "".join(
        f'<div class="{TILE} flex items-center gap-3 py-[14px] w600:hidden">{_icon(i, t)}<div class="grid"><strong class="font-extrabold leading-tight">{esc(title)}</strong>'
        f'<span class="text-[.88rem] text-ink-3">{esc(sub)}</span></div></div>' for i, t, title, sub in HINTS)
    intro = f"""    <section class="grid grid-cols-3 gap-4 w600:grid-cols-1" aria-labelledby="plan-title">
      <div class="{TILE} col-span-3 w600:col-span-1 bg-glass-hi">
        <h1 id="plan-title" class="mb-2 text-[length:clamp(1.8rem,4vw,2.6rem)] font-extrabold leading-[1.06] tracking-[-.03em]">Выходной день <span class="text-brand">по плану</span></h1>
        <p class="m-0 max-w-[58ch] text-[1.05rem] text-ink-3">Укажите день, бюджет и состав: соберём 2–4 события из афиши, чтобы успеть на все.</p>
      </div>
      {hints}
    </section>"""
    form = f"""    <form method="get" action="/plan" class="mt-4 grid grid-cols-4 gap-4 w1000:grid-cols-2 w600:grid-cols-1">
      <fieldset class="{TILE} col-span-2 m-0 min-w-0 w600:col-span-1">
        {_tile_head("calendar", "tone-theatre", "Когда", "день и свободное время")}
        <div class="grid gap-3">
          {_field("День", "day", values.get("day", ""), "date", "required")}
          <div class="grid grid-cols-2 gap-3">
            {_field("Свободно с", "window_from", values.get("window_from", "10:00"), "time")}
            {_field("Свободно до", "window_to", values.get("window_to", "22:00"), "time")}
          </div>
        </div>
      </fieldset>
      <fieldset class="{TILE} m-0 min-w-0 w1000:col-span-1">
        {_tile_head("pin", "tone-excursion", "Где", "город и дорога")}
        <div class="grid gap-3">
          <label class="{LABEL}"><span>Город</span><select class="{FIELD}" name="city">{city_options}</select></label>
          {_field("Дорога между событиями, мин", "max_travel_min", values.get("max_travel_min", "40"), "number", 'min="0" max="240" inputmode="numeric"')}
        </div>
      </fieldset>
      <fieldset class="{TILE} m-0 min-w-0">
        <legend class="sr-only">Сколько событий</legend>
        {_tile_head("list", "tone-concert", "Сколько событий", "от 2 до 4")}
        {_count_radios(values.get("events", "3"))}
      </fieldset>
      <fieldset class="{TILE} col-span-2 m-0 min-w-0 w600:col-span-1">
        {_tile_head("kids", "tone-kids", "Кто идёт", "взрослые и дети")}
        <div class="grid grid-cols-2 gap-3 w600:grid-cols-1">
          {_field("Взрослых", "adults", values.get("adults", "1"), "number", 'min="1" max="10" inputmode="numeric"')}
          {_field("Возраст детей, через запятую", "children", values.get("children", ""), "text", 'placeholder="например 8, 3"')}
        </div>
      </fieldset>
      <fieldset class="{TILE} m-0 min-w-0">
        {_tile_head("gift", "tone-festival", "Бюджет", "на всех вместе")}
        {_field("Сумма, ₽", "budget", values.get("budget", ""), "number", 'min="0" max="1000000" inputmode="numeric" placeholder="без ограничения"')}
      </fieldset>
      <button type="submit" class="group/btn shine relative inline-flex min-h-[120px] flex-col items-center justify-center gap-2 rounded-card bg-brand p-4 text-[1.15rem] font-extrabold text-white shadow-cta tx-btn hover:scale-[1.03] hover:bg-brand-hover active:scale-[.98] w1000:col-span-2 w1000:min-h-[64px] w600:col-span-1">
        <svg viewBox="0 0 24 24" class="h-7 w-7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
        Составить план
      </button>
    </form>"""
    body = [intro, form]
    if error:
        body.append(f'    <p class="mt-4 rounded-card border border-[rgba(200,30,30,.35)] bg-bad-bg p-4 font-semibold text-bad" role="alert">{esc(error)}</p>')
    if result is not None:
        body.append(_result_html(result))
    return _layout("Выходной день: план из афиши", "План дня из 2–4 событий афиши без пересечений, с оценкой дороги и стоимости",
                   "\n".join(body), path="/plan", robots="noindex", wide=True)


def _result_html(result) -> str:
    ok = result.feasible
    tone = "tone-kids" if ok else "tone-festival"
    border = "border-[rgba(22,128,76,.4)]" if ok else "border-[rgba(200,120,0,.5)]"
    parts = ['    <section class="mt-6" aria-live="polite" aria-label="Ваш план">',
             f'      <div class="{TILE} {border} flex items-start gap-3 bg-glass-hi">{_icon("check" if ok else "sun", tone)}<div class="grid gap-1">'
             f'<strong class="text-[1.1rem] font-extrabold">{esc(result.message)}</strong>']
    if result.suggestions:
        items = "".join(f"<li>{esc(s)}</li>" for s in result.suggestions)
        parts.append(f'        <span class="mt-1 text-[.9rem] font-bold text-ink-3">Что изменить:</span><ul class="m-0 list-disc pl-5 text-ink-2">{items}</ul>')
    parts.append("      </div></div>")
    if result.items:
        count = len(result.items)
        parts.append(f'      <ol class="m-0 mt-4 grid list-none grid-cols-2 gap-4 p-0 w600:grid-cols-1">')
        for index, item in enumerate(result.items):
            event = item.event
            span = " col-span-2 w600:col-span-1" if count % 2 == 1 and index == 0 else ""
            travel = (f'<span class="inline-flex items-center gap-1 rounded-full bg-brand-tint px-3 py-[3px] text-[.82rem] font-bold text-brand-ink">'
                      f'В пути около {item.travel_min} мин</span>' if item.travel_min else "")
            price = f"{item.cost} ₽ на всех" if item.price_known else "цена неизвестна"
            end = f"{item.end:%H:%M}" + (" (оценка)" if item.end_estimated else "")
            place = ", ".join(p for p in (event.venue, event.city) if p)
            parts.append(
                f'        <li class="{TILE_LIFT}{span} grid content-start gap-2 has-[a:focus-visible]:outline has-[a:focus-visible]:outline-[3px] has-[a:focus-visible]:outline-offset-[3px] has-[a:focus-visible]:outline-[rgba(79,70,229,.55)]">'
                f'<div class="flex flex-wrap items-center gap-2"><span class="grid h-9 min-w-9 place-items-center rounded-full bg-brand px-3 text-[.9rem] font-extrabold text-white">{index + 1}</span>'
                f'<span class="text-[1.3rem] font-extrabold tracking-[-.02em] [font-variant-numeric:tabular-nums]">{item.start:%H:%M}–{esc(end)}</span>{travel}</div>'
                f'<a class="text-[1.15rem] font-bold leading-[1.3] text-ink no-underline after:absolute after:inset-0 after:rounded-[inherit] after:content-[\'\'] focus-visible:outline-none" href="/event/{int(event.id)}">{esc(event.title)}</a>'
                f'<p class="m-0 text-[.93rem] text-ink-3">{esc(place)}</p>'
                f'<p class="m-0"><span class="rounded-full border border-stroke bg-white/[.78] px-3 py-[3px] text-[.85rem] font-[750] text-brand-ink">{esc(price)}</span></p></li>')
        parts.append("      </ol>")
        parts.append(f'      <div class="{TILE} mt-4 flex flex-wrap items-center gap-3 bg-glass-hi">{_icon("gift", "tone-festival")}'
                     f'<p class="m-0 text-[1.25rem] font-extrabold tracking-[-.01em]">Итого: {result.total_cost} ₽, дорога около {result.total_travel_min} мин.</p></div>')
        parts.append('      <p class="mx-1 mb-0 mt-3 text-[.85rem] text-ink-3">Дорога оценивается по расстоянию, а где координат нет, по типичным '
                     '20 минутам внутри города: это не маршрут. Цену и время перед выходом сверьте по ссылке на источник.</p>')
    parts.append("    </section>")
    return "\n".join(parts)
