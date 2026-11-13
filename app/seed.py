"""Демонстрационные данные для презентации: python -m app.seed [--reset].

Все события выдуманные и помечены источниками «demo-*»: интерфейс показывает плашку «демо-данные».
Часть событий намеренно приходит из двух «источников» с разными формулировками, чтобы показать дедупликацию.
Даты считаются от сегодняшнего дня, поэтому на ближайшие выходные событий всегда достаточно.
Реальные данные подключаются через python -m app.ingest или POST /api/events.
"""
import argparse
import sys
import zlib
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from .config import now_local
from .ingest import upsert_event
from .models import Event
from .schemas import EventIn

OPERA = "Башкирский государственный театр оперы и балета"
DRAMA = "Башкирский академический театр драмы им. М. Гафури"
PUPPETS = "Башкирский государственный театр кукол"
PHILHARMONIC = "Башкирская государственная филармония"
MUSEUM = "Национальный музей Республики Башкортостан"
ART_MUSEUM = "Художественный музей им. М. В. Нестерова"
LIBRARY = "Национальная библиотека им. А. З. Валиди"
CINEMA = "Кинотеатр «Искра»"
FOLK_HOUSE = "Дом дружбы народов"


def demo_events(today: date) -> list[dict]:
    def ev(day: int, hh: int, mm: int, title: str, *, src: str = "demo-afisha", end_day: int | None = None, **kw) -> dict:
        record = {
            "title": title,
            "start_at": datetime.combine(today + timedelta(days=day), time(hh, mm)),
            "source": src,
            "url": f"https://example.com/demo/{zlib.crc32(f'{title}|{day}'.encode()) % 100000}",
            **kw,
        }
        if end_day is not None:
            record["end_at"] = datetime.combine(today + timedelta(days=end_day), time(20, 0))
        return record

    def weekend(sat: int) -> list[dict]:
        """Субботу и воскресенье (sat — смещение субботы от сегодня в днях) наполняем событиями для семей."""
        sun = sat + 1
        return [
            ev(sat, 10, 0, "Забег «Уфимская осень» 5 км", venue="Парк Победы", category="sport", price_min=0, price_max=0,
               age_limit=0, kids_friendly=True, description="Забег для всей семьи, есть детская дистанция."),
            ev(sat, 10, 30, "Мультфильмы на большом экране", venue=CINEMA, category="cinema", price_min=250, age_limit=0,
               kids_friendly=True),
            ev(sat, 11, 0, "Мастер-класс по росписи пряников для детей", venue="Центр творчества «Радуга»", category="kids",
               price_min=300, age_limit=6),
            ev(sat, 11, 0, "Экскурсия «Старая Уфа»", venue="Сбор у памятника Салавату Юлаеву", category="excursion",
               price_min=600, age_limit=6, kids_friendly=True),
            ev(sat, 12, 0, "Спектакль «Золотой ключик»", venue=PUPPETS, category="kids", price_min=400, price_max=600,
               age_limit=3, description="Кукольная сказка для самых маленьких."),
            ev(sat, 12, 0, "Золотой ключик (кукольный спектакль)", src="demo-kukly", venue=PUPPETS, price_min=400, age_limit=3),
            ev(sat, 14, 0, "Семейный квест в музее", venue=MUSEUM, category="kids", price_min=500, price_max=700, age_limit=6),
            ev(sat, 14, 0, "Семейный квест в музее: тайны экспозиции", src="demo-museum", venue=MUSEUM, price_min=500,
               age_limit=6),
            ev(sat, 16, 0, "Спектакль «Три медведя»", venue=PUPPETS, category="kids", price_min=300, price_max=500, age_limit=0),
            ev(sun, 12, 0, "Спектакль «Три медведя»", venue=PUPPETS, category="kids", price_min=300, price_max=500, age_limit=0),
            ev(sun, 14, 0, "Мастер-класс по лепке из глины для детей", venue="Центр творчества «Радуга»", category="kids",
               price_min=350, age_limit=5),
            ev(sun, 16, 0, "Концерт для всей семьи «Сказки с оркестром»", venue=PHILHARMONIC, category="concert",
               price_min=200, price_max=600, age_limit=0, kids_friendly=True),
            ev(sun, 11, 0, "День открытых дверей в музее", venue=MUSEUM, category="exhibition", price_min=0, price_max=0,
               age_limit=0, kids_friendly=True),
        ]

    sat0 = (5 - today.weekday()) % 7  # ближайшая суббота (сегодня, если сегодня суббота)
    base = [
        # --- театр ---
        ev(2, 18, 30, "Балет «Лебединое озеро»", venue=OPERA, category="theatre", price_min=600, price_max=3000, age_limit=6,
           description="Классическая постановка на музыку П. И. Чайковского."),
        ev(2, 18, 30, "Лебединое озеро. Балет П. И. Чайковского", src="demo-opera", venue=OPERA, category="theatre",
           price_min=700, price_max=3000, age_limit="6+",
           description="Балет в двух действиях. Продолжительность около трёх часов с антрактом."),
        ev(5, 18, 0, "Опера «Евгений Онегин»", venue=OPERA, category="theatre", price_min=500, price_max=2500, age_limit=12),
        ev(3, 18, 30, "Спектакль «Ревизор»", venue=DRAMA, category="theatre", price_min=500, price_max=1500, age_limit=16,
           status="sold_out"),
        ev(4, 18, 30, "Спектакль «Вишнёвый сад»", venue=DRAMA, category="theatre", price_min=500, price_max=1400, age_limit=12,
           description="Комедия А. П. Чехова."),
        ev(4, 18, 30, "Вишнёвый сад", src="demo-drama", venue=DRAMA, price_min=500, age_limit=12, status="cancelled",
           description="Спектакль отменён по болезни артиста, билеты вернут."),
        ev(9, 18, 0, "Спектакль «Свадьба Кречинского»", city="Стерлитамак", venue="Стерлитамакский драматический театр",
           category="theatre", price_min=400, price_max=900, age_limit=12),
        ev(12, 18, 0, "Спектакль «Дядя Ваня»", city="Сибай", venue="Сибайский театр драмы", category="theatre",
           price_min=300, price_max=600, age_limit=12),
        # --- концерты ---
        ev(4, 19, 0, "Концерт симфонического оркестра «Времена года»", venue=PHILHARMONIC, category="concert",
           price_min=300, price_max=1200, age_limit=6),
        ev(6, 20, 0, "Джазовый вечер", venue="Концертный зал «Башкортостан»", category="concert", price_min=800,
           price_max=2000, age_limit=16),
        ev(8, 18, 0, "Концерт народной музыки «Курай и кубыз»", venue=FOLK_HOUSE, category="concert", price_min=0,
           price_max=0, age_limit=0, description="Вход свободный по регистрации."),
        ev(10, 17, 0, "Концерт «Осенний вальс»", city="Салават", venue="Дворец культуры", category="concert",
           price_min=300, price_max=500, age_limit=0),
        ev(7, 18, 0, "Вечер романсов", city="Нефтекамск", venue="Городской Дом культуры", category="concert",
           price_min=200, age_limit=6),
        # --- выставки (многодневные) ---
        ev(-3, 0, 0, "Выставка «Башкирский орнамент»", venue=MUSEUM, category="exhibition", price_min=100, price_max=200,
           age_limit=0, end_day=20),
        ev(-10, 0, 0, "Фотовыставка «Урал глазами путешественников»", venue=ART_MUSEUM, category="exhibition",
           price_min=0, price_max=0, age_limit=0, end_day=30),
        ev(-2, 0, 0, "Выставка детских рисунков «Моя Башкирия»", venue=LIBRARY, category="exhibition", price_min=0,
           price_max=0, age_limit=0, end_day=12, kids_friendly=True),
        ev(0, 0, 0, "Выставка авторских кукол", city="Стерлитамак", venue="Краеведческий музей", category="exhibition",
           price_min=150, age_limit=0, end_day=14, kids_friendly=True),
        ev(1, 0, 0, "Выставка «Мёд Башкирии»", venue=MUSEUM, category="exhibition", age_limit=0, end_day=10),  # цена неизвестна
        # --- фестивали ---
        ev(8, 11, 0, "Фестиваль башкирской кухни", venue="Парк имени Якутова", category="festival", price_min=0,
           price_max=0, age_limit=0, end_day=9, kids_friendly=True),
        ev(11, 12, 0, "Фестиваль уличной еды", city="Салават", venue="Центральная площадь", category="festival",
           price_min=0, price_max=0, age_limit=0),
        # --- лекции, кино, экскурсии, спорт ---
        ev(3, 18, 0, "Лекция «История Уфы в лицах»", venue=LIBRARY, category="lecture", price_min=0, price_max=0, age_limit=12),
        ev(6, 17, 0, "Мастер-класс по игре на курае", venue=FOLK_HOUSE, category="lecture", price_min=500, age_limit=12),
        ev(5, 19, 0, "Вечер башкирского кино", venue=CINEMA, category="cinema", price_min=200, age_limit=12),
        ev(11, 11, 0, "Экскурсия по старому Белорецку", city="Белорецк", venue="Краеведческий музей", category="excursion",
           price_min=300, age_limit=6),
        ev(13, 12, 0, "Открытый турнир по самбо", venue="Дворец спорта", category="sport", price_min=0, price_max=0, age_limit=0),
        # --- прошедшее: в выдаче по умолчанию не появится ---
        ev(-1, 19, 0, "Концерт «Осенний бал»", venue=PHILHARMONIC, category="concert", price_min=300, age_limit=0),
    ]
    return base + weekend(sat0) + weekend(sat0 + 7)


def seed(session, today: date | None = None) -> dict[str, int]:
    today = today or now_local().date()
    stats = {"created": 0, "merged": 0, "unchanged": 0}
    for record in demo_events(today):
        _, outcome = upsert_event(session, EventIn.model_validate(record))
        stats[outcome] += 1
    return stats


def reset_demo(session) -> int:
    """Удаляет только демо-события (все источники начинаются с «demo»); реальные данные не трогает."""
    removed = 0
    for event in session.scalars(select(Event)):
        sources = event.sources or []
        if sources and all(str(s.get("name", "")).startswith("demo") for s in sources):
            session.delete(event)
            removed += 1
    session.commit()
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Загрузить демо-данные для презентации")
    parser.add_argument("--reset", action="store_true", help="сначала удалить прежние демо-события")
    args = parser.parse_args(argv)

    from .db import SessionLocal, init_db

    init_db()
    with SessionLocal() as session:
        if args.reset:
            print(f"Удалено демо-событий: {reset_demo(session)}")
        stats = seed(session)
    print(f"Создано: {stats['created']}, склеено дублей: {stats['merged']}, без изменений: {stats['unchanged']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
