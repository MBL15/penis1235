"""Планировщик «Выходной день» (ТЗ §6.5): из событий одного дня собирает 2–4, не пересекающихся по времени, с оценкой
перемещений и общей стоимостью. Работает без ИИ: перебор по данным афиши.

Вход: день, окно времени, бюджет на всех, состав (взрослые и возраст детей), город, максимум времени на один переезд.
Выход: события по порядку, время в пути между ними, стоимость каждого и итог. Если ограничения несовместимы, называется то, что
мешает, и предлагается минимальное изменение (поднять бюджет, расширить окно, разрешить дорогу подольше, убрать событие).

Как оценивается дорога: то же место 0 минут; у обоих событий есть координаты: расстояние по прямой со скоростью 25 км/ч плюс 5 минут;
координат нет: 20 минут по городу и 90 между городами. Это оценка, а не маршрут: для неё нет сервиса карт."""
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from .config import now_local
from .models import Event
from .search import SearchFilters, search

MIN_EVENTS, MAX_EVENTS = 2, 4
BUFFER_MIN = 10  # запас между окончанием одного и началом следующего: войти, найти место
SAME_CITY_TRAVEL, OTHER_CITY_TRAVEL = 20, 90
CITY_SPEED_KMH = 25.0
MAX_CANDIDATES = 60
DEFAULT_DURATION = {"exhibition": 60, "excursion": 90, "theatre": 150, "concert": 120, "kids": 60}
FALLBACK_DURATION = 90
WINDOW_ALL = (time(0, 0), time(23, 59))


@dataclass
class PlanRequest:
    day: date
    window_from: time = time(10, 0)
    window_to: time = time(22, 0)
    budget: int | None = None  # рублей на всех; None — без ограничения
    adults: int = 1
    children: list[int] = field(default_factory=list)  # возраст каждого ребёнка
    city: str | None = None
    max_travel_min: int | None = 40  # на один переезд; None — без ограничения
    events: int = 3
    category: str | None = None

    @property
    def people(self) -> int:
        return self.adults + len(self.children)

    def describe(self) -> list[str]:
        parts = [f"{self.day:%d.%m.%Y}", f"окно {self.window_from:%H:%M}–{self.window_to:%H:%M}"]
        if self.city:
            parts.append(f"город {self.city}")
        if self.budget is not None:
            parts.append(f"бюджет {self.budget} ₽ на {self.people}")
        if self.children:
            parts.append("дети " + ", ".join(f"{a} л." for a in self.children))
        if self.max_travel_min is not None:
            parts.append(f"дорога до {self.max_travel_min} мин")
        return parts


@dataclass
class PlanItem:
    event: Event
    start: datetime
    end: datetime
    end_estimated: bool
    travel_min: int  # от предыдущего события; у первого 0
    cost: int  # на всех, рублей
    price_known: bool


@dataclass
class PlanResult:
    request: PlanRequest
    items: list[PlanItem] = field(default_factory=list)
    feasible: bool = False
    message: str = ""
    suggestions: list[str] = field(default_factory=list)

    @property
    def total_cost(self) -> int:
        return sum(i.cost for i in self.items)

    @property
    def total_travel_min(self) -> int:
        return sum(i.travel_min for i in self.items)


@dataclass
class _Slot:
    event: Event
    start: datetime
    end: datetime
    estimated: bool
    cost: int
    price_known: bool
    score: float


def _haversine_km(a: Event, b: Event) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def travel_minutes(a: Event, b: Event) -> int:
    """Оценка времени на дорогу от места события a до места события b (см. описание модуля)."""
    if (a.venue_key and a.venue_key == b.venue_key) or (a.venue and a.venue == b.venue and a.city == b.city):
        return 0
    if None not in (a.lat, a.lon, b.lat, b.lon):
        return round(_haversine_km(a, b) / CITY_SPEED_KMH * 60 + 5)
    return SAME_CITY_TRAVEL if a.city == b.city else OTHER_CITY_TRAVEL


def _slot(event: Event, people: int) -> _Slot | None:
    start = event.start_at
    if (start.hour, start.minute) == (0, 0):
        return None  # время начала не указано: в расписание такое событие не поставить
    estimated = False
    if event.end_at and event.end_at.date() == start.date() and event.end_at > start:
        end = event.end_at
    else:
        minutes = event.duration_min or DEFAULT_DURATION.get(event.category, FALLBACK_DURATION)
        estimated = not event.duration_min
        end = start + timedelta(minutes=minutes)
    price_known = event.price_min is not None
    cost = (event.price_min or 0) * people
    score = (event.confidence or 0.5) + (0.1 if event.verified_at else 0.0)
    return _Slot(event, start, end, estimated, cost, price_known, score)


def _fits_window(slot: _Slot, req: PlanRequest) -> bool:
    return slot.start.time() >= req.window_from and slot.end <= datetime.combine(slot.start.date(), req.window_to)


def _best_plan(slots: list[_Slot], req: PlanRequest, count: int, *, budget: int | None, max_travel: int | None) -> list[_Slot] | None:
    """Лучшая цепочка из count событий: меньше стоимость, затем меньше дорога, затем выше уверенность. Перебор с отсечениями."""
    slots = sorted(slots, key=lambda s: s.start)
    best: tuple | None = None
    best_chain: list[_Slot] | None = None

    def gap_ok(a: _Slot, b: _Slot) -> int | None:
        travel = travel_minutes(a.event, b.event)
        if max_travel is not None and travel > max_travel:
            return None
        return travel if a.end + timedelta(minutes=travel + BUFFER_MIN) <= b.start else None

    def go(chain: list[_Slot], cost: int, travel: int, index: int) -> None:
        nonlocal best, best_chain
        if len(chain) == count:
            key = (cost, travel, -sum(s.score for s in chain))
            if best is None or key < best:
                best, best_chain = key, list(chain)
            return
        for j in range(index, len(slots)):
            nxt = slots[j]
            if len(slots) - j < count - len(chain):
                break
            if budget is not None and cost + nxt.cost > budget:
                continue
            step = 0
            if chain:
                step = gap_ok(chain[-1], nxt)
                if step is None:
                    continue
            go(chain + [nxt], cost + nxt.cost, travel + step, j + 1)

    go([], 0, 0, 0)
    return best_chain


def _candidates(session: Session, req: PlanRequest, now: datetime) -> list[_Slot]:
    f = SearchFilters(city=req.city, category=req.category, date_from=req.day, date_to=req.day, limit=200)
    if req.children:
        f.age = min(req.children)  # подходить должно самому младшему
        f.kids = None
    events, _ = search(session, f, now)
    slots = [s for s in (_slot(e, req.people) for e in events) if s is not None]
    slots.sort(key=lambda s: (-s.score, s.start))
    return slots[:MAX_CANDIDATES]


def _to_items(chain: list[_Slot]) -> list[PlanItem]:
    items = []
    for i, slot in enumerate(chain):
        travel = travel_minutes(chain[i - 1].event, slot.event) if i else 0
        items.append(PlanItem(slot.event, slot.start, slot.end, slot.estimated, travel, slot.cost, slot.price_known))
    return items


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def plan_weekend(session: Session, req: PlanRequest, now: datetime | None = None) -> PlanResult:
    """Собирает план дня. Если 2–4 событий вместе не получается, возвращает feasible=False с причиной и предложениями."""
    now = now or now_local()
    count = max(MIN_EVENTS, min(MAX_EVENTS, req.events))
    result = PlanResult(req)
    everything = _candidates(session, req, now)
    in_window = [s for s in everything if _fits_window(s, req)]

    chain, got = None, 0
    for n in range(count, MIN_EVENTS - 1, -1):
        chain = _best_plan(in_window, req, n, budget=req.budget, max_travel=req.max_travel_min)
        if chain:
            got = n
            break
    if chain:
        result.items, result.feasible = _to_items(chain), True
        if got < count:
            result.message = f"Нашлось {got} из {count} событий: все три условия вместе дают только столько."
            result.suggestions = _suggest(everything, req, got + 1, chain)
        else:
            unknown = [i.event.title for i in result.items if not i.price_known]
            result.message = f"План из {got} событий: стоимость {result.total_cost} ₽, дорога {result.total_travel_min} мин."
            if unknown:
                result.message += f" Цена неизвестна у: {', '.join(unknown)}: в сумму она не входит."
            if any(i.end_estimated for i in result.items):
                result.message += " Окончание оценено по типичной длительности."
        return result

    if not everything:
        result.message = ("В этот день подходящих событий нет: " + ("для детей такого возраста, " if req.children else "") +
                          "с известным временем начала в этом городе. Попробуйте другой день или город.")
        return result
    result.message = "Составить план из 2 событий при этих ограничениях не получается."
    result.suggestions = _suggest(everything, req, MIN_EVENTS, None)
    if not result.suggestions:
        result.message += " Подходящие события в этот день не совмещаются по времени."
    return result


def _suggest(everything: list[_Slot], req: PlanRequest, need: int, current: list[_Slot] | None) -> list[str]:
    """Что изменить, чтобы получилось need событий: по одному условию, самое малое изменение первым."""
    options: list[tuple[float, str]] = []
    window_ok = [s for s in everything if _fits_window(s, req)]

    # 1) расширить окно времени
    chain = _best_plan(everything, req, need, budget=req.budget, max_travel=req.max_travel_min)
    if chain and len(window_ok) < len(everything):
        start, end = chain[0].start.time(), chain[-1].end.time()
        extra = max(0, _minutes(req.window_from) - _minutes(start)) + max(0, _minutes(end) - _minutes(req.window_to))
        options.append((extra / 60.0, f"расширить окно времени до {start:%H:%M}–{end:%H:%M}"))
    # 2) поднять бюджет
    if req.budget is not None:
        chain = _best_plan(window_ok, req, need, budget=None, max_travel=req.max_travel_min)
        if chain:
            needed = sum(s.cost for s in chain)
            options.append(((needed - req.budget) / max(req.budget, 1) * 3, f"поднять бюджет с {req.budget} до {needed} ₽ (+{needed - req.budget} ₽)"))
    # 3) разрешить дорогу подольше
    if req.max_travel_min is not None:
        chain = _best_plan(window_ok, req, need, budget=req.budget, max_travel=None)
        if chain:
            longest = max((travel_minutes(a.event, b.event) for a, b in zip(chain, chain[1:])), default=0)
            options.append(((longest - req.max_travel_min) / 30.0, f"разрешить дорогу до {longest} мин между событиями (сейчас до {req.max_travel_min})"))
    # 4) убрать одно событие
    if need > MIN_EVENTS and current:
        options.append((1.5, f"остановиться на {len(current)} событиях"))
    options.sort()
    return [text[0].upper() + text[1:] for _, text in options[:3]]


def parse_budget_text(text: str) -> tuple[int | None, int | None]:
    """«2000 рублей на двоих» → (2000, 2); «до 1500 ₽» → (1500, None). Для команды /plan в боте."""
    import re

    from .rules import PEOPLE

    low = (text or "").lower().replace("ё", "е")
    money = re.search(r"(\d[\d\s]{1,6})\s*(?:₽|руб\w*|р\b)", low)
    people = next((n for word, n in PEOPLE.items() if word in low), None)
    return (int(money.group(1).replace(" ", "")) if money else None), people
