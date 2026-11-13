"""HTML-страницы, которые сервер собирает сам: карточка события с постоянной ссылкой (ТЗ §6.3).

Страница собирается на сервере, а не скриптом: ссылкой можно поделиться, у неё есть заголовок и описание для предпросмотра,
она открывается без JavaScript, а отменённое событие видно с явной пометкой. Стили — те же утилиты Tailwind, что и на главной
(tailwind.config.js просматривает и этот файл)."""
import html
import json
from datetime import datetime

from . import config
from .config import now_local
from .formatting import MONTHS_GEN, duration_text, is_demo, is_free, is_past, price_text, when_text
from .models import Event
from .normalize import CATEGORIES, LANGUAGES, STATUSES

BUTTON = ("inline-flex min-h-[50px] items-center justify-center gap-2 rounded-ctl px-6 font-bold no-underline tx-btn "
          "w600:w-full")
PRIMARY = f"{BUTTON} bg-brand text-white shadow-cta hover:bg-brand-hover"
SECONDARY = f"{BUTTON} border border-stroke bg-glass-hi text-brand-ink shadow-surface hover:bg-white"
CHIP = "rounded-full border border-stroke bg-white/[.78] px-3 py-[3px] text-[.85rem] font-semibold text-ink-2"
BANNERS = {  # статус → оформление плашки: отмена и перенос нельзя пропустить
    "cancelled": "rounded-ctl bg-bad-bg px-4 py-3 font-bold text-bad",
    "rescheduled": "rounded-ctl border border-warn-line bg-warn-bg px-4 py-3 font-bold text-warn",
    "sold_out": "rounded-ctl border border-warn-line bg-warn-bg px-4 py-3 font-bold text-warn",
}
BANNER_TEXT = {
    "cancelled": "Событие отменено. Не планируйте поездку: проверьте новости организатора по ссылке на источник ниже.",
    "rescheduled": "Событие перенесено. Новую дату уточняйте у организатора по ссылке на источник ниже.",
    "sold_out": "Билеты проданы.",
}


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _https(url: str | None) -> str:
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else ""


def _layout(title: str, description: str, body: str, *, path: str = "", image: str = "", robots: str = "") -> str:
    canonical = f"{config.PUBLIC_URL}{path}" if path else ""
    meta = [f'<meta name="description" content="{esc(description)}">',
            f'<meta property="og:title" content="{esc(title)}">',
            f'<meta property="og:description" content="{esc(description)}">',
            '<meta property="og:type" content="website">', '<meta property="og:locale" content="ru_RU">']
    if canonical:
        meta += [f'<link rel="canonical" href="{esc(canonical)}">', f'<meta property="og:url" content="{esc(canonical)}">']
    if _https(image):
        meta.append(f'<meta property="og:image" content="{esc(image)}">')
    if robots:
        meta.append(f'<meta name="robots" content="{esc(robots)}">')
    return f"""<!doctype html>
<html lang="ru" class="scroll-smooth">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>{esc(title)}</title>
  {chr(10).join('  ' + m for m in meta).strip()}
  <script src="/static/boot.js"></script>
  <link rel="stylesheet" href="/static/tailwind.css">
</head>
<body class="min-h-screen bg-art bg-[#eef2fb] bg-fixed text-ink antialiased">
  <header class="mx-auto flex max-w-[820px] items-center justify-between gap-3 px-4 pt-4">
    <a href="/" class="inline-flex min-h-[44px] items-center gap-2 rounded-field border border-stroke bg-glass-hi px-4 font-[650] text-brand-ink no-underline shadow-surface tx-link hover:bg-white">← К афише</a>
    <a href="/docs" class="rounded-[12px] px-[14px] py-2 text-[.95rem] font-semibold text-ink-3 no-underline tx-link hover:bg-white/70 hover:text-ink w600:hidden">API</a>
  </header>
  <main class="mx-auto max-w-[820px] px-4 pb-12 pt-5">
{body}
    <p class="mx-auto mt-6 max-w-[780px] text-center text-[.85rem] text-ink-3">{esc(config.DISCLAIMER)}</p>
  </main>
</body>
</html>
"""


def _row(label: str, value_html: str) -> str:
    return (f'<div class="grid grid-cols-[110px_minmax(0,1fr)] gap-3 w600:grid-cols-1 w600:gap-0">'
            f'<dt class="font-bold text-ink-3">{esc(label)}</dt><dd class="m-0 font-semibold [overflow-wrap:anywhere]">{value_html}</dd></div>')


def _structured_data(event: Event) -> str:
    """Разметка schema.org для поисковиков: не исполняется, на CSP не влияет."""
    mode = {"cancelled": "EventCancelled", "rescheduled": "EventRescheduled"}.get(event.status, "EventScheduled")
    data = {
        "@context": "https://schema.org", "@type": "Event", "name": event.title, "startDate": event.start_at.isoformat(),
        "eventStatus": f"https://schema.org/{mode}",
        "location": {"@type": "Place", "name": event.venue or event.city, "address": event.address or event.city},
    }
    if event.end_at:
        data["endDate"] = event.end_at.isoformat()
    if event.description:
        data["description"] = event.description[:500]
    if _https(event.image_url):
        data["image"] = event.image_url
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")  # ни одного «<» внутри блока: закрыть его раньше времени нельзя


def render_event_page(event: Event, now: datetime | None = None) -> str:
    now = now or now_local()
    category = CATEGORIES.get(event.category, CATEGORIES["other"])
    status = STATUSES.get(event.status, event.status)
    when = when_text(event, now)
    place = ", ".join(p for p in (event.venue, event.city) if p)
    price = price_text(event)
    banner = ""
    if event.status in BANNERS:
        banner = f'<p role="alert" class="m-0 {BANNERS[event.status]}">{esc(status)}. {esc(BANNER_TEXT[event.status])}</p>'
    elif is_past(event, now):
        banner = f'<p class="m-0 {BANNERS["sold_out"]}">Это событие уже прошло.</p>'

    chips = [f'<span class="{CHIP}">{esc(category)}</span>']
    if is_free(event):
        chips.append(f'<span class="{CHIP}">Бесплатно</span>')
    if event.age_limit:
        chips.append(f'<span class="{CHIP}">{int(event.age_limit)}+</span>')
    if event.kids_friendly:
        chips.append(f'<span class="{CHIP}">Детям и семьям</span>')
    if is_demo(event):
        chips.append(f'<span class="{CHIP}">демо: событие выдумано для презентации</span>')

    facts = [_row("Когда", esc(when))]
    if event.series_until and event.series_until.date() > event.start_at.date():
        facts.append(_row("Расписание", "Серия показов: известен один показ, остальные даты источник не сообщает. "
                                        "Уточняйте расписание у организатора."))
    if place:
        facts.append(_row("Где", esc(place) + (f"<br><span class=\"font-normal text-ink-3\">{esc(event.address)}</span>" if event.address else "")))
    if event.duration_min:
        facts.append(_row("Длительность", f"около {esc(duration_text(event.duration_min))}"))
    if event.language in LANGUAGES:
        facts.append(_row("Язык", esc(LANGUAGES[event.language])))
    facts.append(_row("Цена", esc(price)))
    if event.age_limit:
        facts.append(_row("Возраст", f"{int(event.age_limit)}+"))
    facts.append(_row("Статус", esc(status)))

    description = (
        "".join(f'<p class="m-0 mb-3">{esc(par)}</p>' for par in event.description.split("\n\n") if par.strip())
        if event.description else '<p class="m-0 text-ink-3">В источнике нет описания.</p>'
    )

    sources = []
    for src in event.sources or []:
        name, url = esc(src.get("name", "источник")), _https(src.get("url"))
        link = (f'<a class="font-semibold text-brand underline underline-offset-2" href="{esc(url)}" target="_blank" '
                f'rel="noopener noreferrer">{name}<span class="sr-only"> (откроется в новой вкладке)</span></a>') if url else name
        state = STATUSES.get(src.get("status"), "") or "статус источником не указан"
        sources.append(f'<li class="m-0">{link} <span class="text-ink-3">· {esc(state)}</span></li>')
    source_block = ""
    if sources:
        count = len(sources)
        source_block = (f'<section class="mt-6" aria-labelledby="src-title"><h2 id="src-title" class="m-0 mb-2 text-[.76rem] font-extrabold '
                        f'uppercase tracking-[.08em] text-ink-3">Откуда информация{" · подтверждено " + str(count) + " источниками" if count > 1 else ""}</h2>'
                        f'<ul class="m-0 grid list-none gap-2 p-0">{"".join(sources)}</ul></section>')

    actions = []
    if _https(event.url):
        actions.append(f'<a class="{PRIMARY}" href="{esc(_https(event.url))}" target="_blank" rel="noopener noreferrer">'
                       f'Источник и билеты<span class="sr-only"> (откроется в новой вкладке)</span></a>')
    actions.append(f'<a class="{SECONDARY}" href="/feedback?event={int(event.id)}">Сообщить об ошибке</a>')

    updated = event.updated_at or event.created_at
    stamp = f"Данные обновлены {updated.day} {MONTHS_GEN[updated.month - 1]} {updated.year}." if updated else ""
    title = f"{'ОТМЕНЕНО: ' if event.status == 'cancelled' else ''}{event.title} — Афиша культуры Башкортостана"
    summary = " · ".join(p for p in (when, place, price) if p)

    body = f"""    <article class="grid gap-4 rounded-panel border border-stroke bg-glass-hi p-6 shadow-surface w600:p-4">
      {banner}
      <div class="flex flex-wrap gap-2">{"".join(chips)}</div>
      <h1 class="m-0 text-[length:clamp(1.6rem,4vw,2.4rem)] font-extrabold leading-[1.15] tracking-[-.02em] [overflow-wrap:anywhere] [text-wrap:balance]{' line-through decoration-[2px]' if event.status == 'cancelled' else ''}">{esc(event.title)}</h1>
      <dl class="m-0 grid gap-3">{"".join(facts)}</dl>
      <div class="text-ink-2">{description}</div>
      {source_block}
      <div class="mt-2 flex flex-wrap gap-3">{"".join(actions)}</div>
      <p class="m-0 text-[.85rem] text-ink-3">{esc(stamp)} Постоянная ссылка на эту карточку: <a class="text-brand underline underline-offset-2" href="/event/{int(event.id)}">/event/{int(event.id)}</a></p>
    </article>
    <script type="application/ld+json">{_structured_data(event)}</script>"""
    return _layout(title, summary or event.title, body, path=f"/event/{int(event.id)}", image=event.image_url or "")


def render_not_found_page() -> str:
    body = """    <article class="grid gap-4 rounded-panel border border-stroke bg-glass-hi p-6 text-center shadow-surface">
      <h1 class="m-0 text-[1.6rem] font-extrabold">Такого события нет</h1>
      <p class="m-0 text-ink-2">Возможно, ссылка неверна или событие убрали из афиши. Посмотрите актуальную афишу.</p>
      <div><a class="inline-flex min-h-[50px] items-center justify-center gap-2 rounded-ctl bg-brand px-6 font-bold text-white no-underline shadow-cta tx-btn hover:bg-brand-hover" href="/">Открыть афишу</a></div>
    </article>"""
    return _layout("Событие не найдено — Афиша культуры Башкортостана", "Такого события нет в афише.", body, robots="noindex")
