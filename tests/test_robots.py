"""Разбор robots.txt (app/sources/robots.py, К-6 ревью 14.11.2026): подстановки, самое длинное правило, «Disallow: /?»."""
from app.sources.robots import Robots

CULTURE = "User-agent: *\nDisallow: /?\nDisallow: /search\nDisallow: /*city=*\n"


def test_question_mark_rule_does_not_block_the_whole_site():
    robots = Robots(CULTURE)
    assert robots.can_fetch("bot/1.0", "https://www.culture.ru/afisha/respublika-bashkortostan")
    assert robots.can_fetch("bot/1.0", "https://www.culture.ru/afisha/respublika-bashkortostan?page=2")
    assert not robots.can_fetch("bot/1.0", "https://www.culture.ru/?utm=1")
    assert not robots.can_fetch("bot/1.0", "https://www.culture.ru/search?q=jazz")
    assert not robots.can_fetch("bot/1.0", "https://www.culture.ru/afisha?city=ufa")


def test_longest_rule_wins_and_allow_breaks_ties():
    robots = Robots("User-agent: *\nDisallow: /afisha\nAllow: /afisha/respublika\nDisallow: /*.pdf$\n")
    assert not robots.can_fetch("x", "https://s/afisha/moskva")
    assert robots.can_fetch("x", "https://s/afisha/respublika-bashkortostan")
    assert not robots.can_fetch("x", "https://s/files/a.pdf") and robots.can_fetch("x", "https://s/files/a.pdf?x=1")


def test_specific_agent_group_and_empty_rules():
    robots = Robots("User-agent: bashafisha-hackathon\nDisallow: /private\n\nUser-agent: *\nDisallow: /\n")
    assert robots.can_fetch("bashafisha-hackathon/1.0 (+https://x)", "https://s/afisha")
    assert not robots.can_fetch("bashafisha-hackathon/1.0", "https://s/private/1")
    assert not robots.can_fetch("other", "https://s/afisha")
    assert Robots("User-agent: *\nDisallow:\n").can_fetch("x", "https://s/anything")
    assert Robots("").can_fetch("x", "https://s/anything")
