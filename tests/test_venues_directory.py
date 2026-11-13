"""Справочник площадок с синонимами (ТЗ §6.5, §8.2): одна площадка под разными названиями источников."""
import json
from datetime import datetime, timedelta

import pytest

from app import venues as directory
from app.config import now_local
from app.maintenance import main as maintenance_main
from app.search import SearchFilters, search, venues

from .test_formatting_and_bot import _Ctx


def soon(days, hour=19):
    return (now_local() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


@pytest.fixture
def places(add):
    add("Концерт А", soon(2), venue="ГКЗ «Башкортостан»", city="Уфа", category="concert")
    add("Концерт Б", soon(3), venue="Государственный концертный зал «Башкортостан»", city="Уфа", category="concert")
    add("Концерт В", soon(4), venue="ГКЗ Башкортостан", city="Уфа", category="concert")
    add("Спектакль Г", soon(3, 18), venue="Театр драмы им. Гафури", city="Уфа", category="theatre")
    add("Спектакль Д", soon(4, 18), venue="Башкирский ордена Трудового Красного Знамени академический театр драмы им. Мажита Гафури",
        city="Уфа", category="theatre")
    add("Лекция Е", soon(5), venue="Неизвестная площадка", city="Уфа", category="lecture")


def test_synonyms_share_one_key_and_unknown_venues_keep_plain_normalization():
    key = directory.venue_key_for("Государственный концертный зал «Башкортостан»")
    assert directory.venue_key_for("ГКЗ «Башкортостан»") == directory.venue_key_for("гкз башкортостан") == key
    assert directory.venue_key_for("Дворец Молодёжи") == directory.venue_key_for("дворец молодежи!!") == "дворец молодежи"
    assert directory.venue_key_for("") == directory.venue_key_for(None) == ""
    assert directory.canonical_venue("ГКЗ Башкортостан") == "Государственный концертный зал «Башкортостан»"
    assert directory.canonical_venue("Неизвестная площадка") is None


def test_directory_groups_synonyms_in_the_venue_list_under_the_official_name(session, places):
    found = {v["name"]: v for v in venues(session, now_local())}
    gkz = found["Государственный концертный зал «Башкортостан»"]
    assert gkz["count"] == 3 and set(gkz["names"]) == {"ГКЗ «Башкортостан»", "ГКЗ Башкортостан"}
    drama = found["Башкирский академический театр драмы им. Мажита Гафури"]
    assert drama["count"] == 2 and "Театр драмы им. Гафури" in drama["names"]
    assert found["Неизвестная площадка"]["names"] == []


def test_filter_finds_events_under_any_spelling_including_a_short_synonym(session, places):
    def titles(**kw):
        found, _ = search(session, SearchFilters(**kw), now_local())
        return {e.title for e in found}

    assert titles(venue="ГКЗ Башкортостан") == {"Концерт А", "Концерт Б", "Концерт В"}
    assert titles(venue="концертный зал") == {"Концерт А", "Концерт Б", "Концерт В"}
    assert titles(venue="Театр драмы им. Гафури") == {"Спектакль Г", "Спектакль Д"}
    assert titles(venue="неизвестная") == {"Лекция Е"}


def test_api_directory_uses_official_names(client, places):
    listing = client.get("/api/v1/venues").json()
    assert listing[0]["name"] == "Государственный концертный зал «Башкортостан»" and listing[0]["count"] == 3
    events = client.get("/api/v1/events", params={"venue": "ГКЗ «Башкортостан»"}).json()
    assert events["total"] == 3


def test_a_missing_or_broken_file_falls_back_to_plain_normalization(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(directory, "VENUES_FILE", tmp_path / "nope.json")
    assert directory.venue_key_for("ГКЗ «Башкортостан»") == "гкз башкортостан"
    broken = tmp_path / "venues.json"
    broken.write_text("{oops", encoding="utf-8")
    monkeypatch.setattr(directory, "VENUES_FILE", broken)
    assert directory.venue_key_for("ГКЗ «Башкортостан»") == "гкз башкортостан"
    assert any("Справочник площадок не прочитан" in r.message for r in caplog.records)


def test_the_file_is_reread_after_an_edit_and_bad_entries_are_skipped(tmp_path, monkeypatch):
    path = tmp_path / "venues.json"
    path.write_text(json.dumps({"venues": [{"name": "Зал А", "aliases": ["А-зал"]}, {"aliases": ["без названия"]}, "строка"]}), encoding="utf-8")
    monkeypatch.setattr(directory, "VENUES_FILE", path)
    assert directory.venue_key_for("А-зал") == "зал а"
    path.write_text(json.dumps([{"name": "Зал Б", "aliases": ["Б-зал"]}]), encoding="utf-8")  # допустим и список без обёртки
    import os

    os.utime(path, (os.path.getmtime(path) + 5, os.path.getmtime(path) + 5))
    assert directory.venue_key_for("Б-зал") == "зал б" and directory.venue_key_for("А-зал") == "а зал"


def test_maintenance_recomputes_keys_after_the_directory_changes(session, add, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(directory, "VENUES_FILE", tmp_path / "nope.json")  # справочника ещё нет: ключи простые
    first, _ = add("Концерт А", soon(2), venue="ГКЗ Башкортостан")
    second, _ = add("Концерт Б", soon(3), venue="Государственный концертный зал «Башкортостан»")
    assert first.venue_key != second.venue_key
    monkeypatch.undo()  # справочник появился
    monkeypatch.setattr("app.db.SessionLocal", lambda: _Ctx(session))
    monkeypatch.setattr("app.db.init_db", lambda: None)
    assert maintenance_main(["venues"]) == 0
    assert "изменится" in capsys.readouterr().out and first.venue_key != second.venue_key
    assert maintenance_main(["venues", "--apply"]) == 0
    assert "пересчитан у событий: 1" in capsys.readouterr().out  # у написания по канонической форме ключ уже совпадал
    session.refresh(first)
    session.refresh(second)
    assert first.venue_key == second.venue_key
    assert maintenance_main(["venues"]) == 0 and "событий: 0" in capsys.readouterr().out
