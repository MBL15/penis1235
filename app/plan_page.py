"""Страница «Выходной день» (/plan): форма и результат планировщика. Всё на сервере: параметры в адресной строке, результат
открывается по ссылке и работает без JavaScript."""
from datetime import date, time, timedelta

from sqlalchemy.orm import Session

from .config import now_local
from .normalize import canonical_city
from .pages import PRIMARY, _layout, esc
from .planner import PlanRequest, plan_weekend
from .search import facets

FIELD = ("min-h-[46px] w-full rounded-field border border-stroke bg-white/90 px-3 text-[1rem] text-ink "
         "focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/30")
LABEL = "grid gap-[6px] text-[.88rem] font-bold text-ink-3"


class FormError(ValueError):
    """Ошибка в значениях формы: текст показывается человеку как есть."""


def _field(label: str, name: str, value: str, kind: str = "text", extra: str = "") -> str:
    return (f'<label class="{LABEL}"><span>{esc(label)}</span>'
            f'<input class="{FIELD}" type="{kind}" name="{name}" value="{esc(value)}" {extra}></label>')


def default_values(today: date, query) -> dict:
    saturday = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
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
    today = now_local().date()
    values = default_values(today, query)
    cities = [c["name"] for c in facets(session)["cities"]]
    if "day" not in query:  # первое открытие: только форма
        return render(values, cities)
    try:
        request = parse_request(values, cities, today)
    except FormError as exc:
        return render(values, cities, error=str(exc))
    return render(values, cities, plan_weekend(session, request))


def render(values: dict, cities: list[str], result=None, error: str = "") -> str:
    options = "".join(f'<option value="{esc(c)}"{" selected" if c == values.get("city") else ""}>{esc(c)}</option>' for c in cities)
    city_options = f'<option value="">Любой город</option>{options}'
    count_options = "".join(f'<option value="{n}"{" selected" if str(n) == str(values.get("events")) else ""}>{n}</option>' for n in (2, 3, 4))
    form = f"""    <h1 class="mb-2 text-[1.7rem] font-[800] leading-tight tracking-[-.02em]">Выходной день</h1>
    <p class="mb-4 text-ink-2">Укажите день, бюджет и состав: соберём 2–4 события без пересечений по времени, с оценкой дороги и общей стоимостью.</p>
    <form method="get" action="/plan" class="grid gap-3 rounded-panel border border-stroke bg-glass p-4 shadow-surface">
      <div class="grid grid-cols-2 gap-3 w600:grid-cols-1">
        {_field("День", "day", values.get("day", ""), "date", "required")}
        <label class="{LABEL}"><span>Город</span><select class="{FIELD}" name="city">{city_options}</select></label>
        {_field("Свободно с", "window_from", values.get("window_from", "10:00"), "time")}
        {_field("Свободно до", "window_to", values.get("window_to", "22:00"), "time")}
        {_field("Бюджет на всех, ₽", "budget", values.get("budget", ""), "number", 'min="0" max="1000000" inputmode="numeric" placeholder="без ограничения"')}
        {_field("Взрослых", "adults", values.get("adults", "1"), "number", 'min="1" max="10" inputmode="numeric"')}
        {_field("Возраст детей, через запятую", "children", values.get("children", ""), "text", 'placeholder="например 8, 3"')}
        {_field("Дорога между событиями, мин", "max_travel_min", values.get("max_travel_min", "40"), "number", 'min="0" max="240" inputmode="numeric"')}
        <label class="{LABEL}"><span>Сколько событий</span><select class="{FIELD}" name="events">{count_options}</select></label>
      </div>
      <button type="submit" class="{PRIMARY}">Составить план</button>
    </form>"""
    body = [form]
    if error:
        body.append(f'    <p class="mt-4 rounded-field border border-[rgba(200,30,30,.35)] bg-white/80 p-3 font-semibold text-ink" role="alert">{esc(error)}</p>')
    if result is not None:
        body.append(_result_html(result))
    return _layout("Выходной день: план из афиши", "План дня из 2–4 событий афиши без пересечений, с оценкой дороги и стоимости",
                   "\n".join(body), path="/plan", robots="noindex")


def _result_html(result) -> str:
    border = "border-[rgba(22,128,76,.4)]" if result.feasible else "border-[rgba(200,120,0,.5)]"
    parts = [f'    <section class="mt-5 rounded-panel border {border} bg-glass p-4 shadow-surface" aria-live="polite">',
             f'      <p class="m-0 font-bold">{esc(result.message)}</p>']
    if result.suggestions:
        items = "".join(f"<li>{esc(s)}</li>" for s in result.suggestions)
        parts.append(f'      <p class="mb-1 mt-3 text-[.9rem] font-bold text-ink-3">Что изменить:</p><ul class="m-0 pl-5">{items}</ul>')
    if result.items:
        parts.append('      <ol class="m-0 mt-4 grid list-none gap-3 p-0">')
        for item in result.items:
            event = item.event
            travel = f'<div class="text-[.85rem] text-ink-3">В пути от предыдущего: около {item.travel_min} мин</div>' if item.travel_min else ""
            price = f"{item.cost} ₽ на всех" if item.price_known else "цена неизвестна"
            end = f"{item.end:%H:%M}" + (" (оценка)" if item.end_estimated else "")
            place = ", ".join(p for p in (event.venue, event.city) if p)
            parts.append(
                f'        <li class="rounded-field border border-stroke bg-white/80 p-3">{travel}'
                f'<div class="font-[750]">{item.start:%H:%M}–{esc(end)} · <a class="text-brand-ink underline" href="/event/{int(event.id)}">{esc(event.title)}</a></div>'
                f'<div class="text-[.9rem] text-ink-2">{esc(place)} · {esc(price)}</div></li>')
        parts.append("      </ol>")
        parts.append(f'      <p class="mb-0 mt-4 font-bold">Итого: {result.total_cost} ₽, дорога около {result.total_travel_min} мин.</p>')
        parts.append('      <p class="mb-0 mt-2 text-[.82rem] text-ink-3">Дорога оценивается по расстоянию, а где координат нет, по типичным '
                     '20 минутам внутри города: это не маршрут. Цену и время перед выходом сверьте по ссылке на источник.</p>')
    parts.append("    </section>")
    return "\n".join(parts)
