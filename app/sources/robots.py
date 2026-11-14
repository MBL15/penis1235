"""Разбор robots.txt по RFC 9309 (как у Google и Яндекса): подстановки `*` и `$`, самое длинное совпадение, Allow при равенстве.

Стандартный `urllib.robotparser` подстановок не понимает, а правило `Disallow: /?` на Python 3.10–3.12.3 читает как запрет всего
сайта (из правила выбрасывается пустая строка запроса, остаётся `/`). На culture.ru такое правило есть, и обход афиши
останавливался, хотя сайт его не запрещает. Здесь правила сравниваются с путём и строкой запроса как есть."""
import re
from urllib.parse import urlsplit


def _pattern(path: str) -> re.Pattern:
    anchored = path.endswith("$")
    body = re.escape(path[:-1] if anchored else path).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


class Robots:
    def __init__(self, text: str = "") -> None:
        self.groups: dict[str, list[tuple[bool, str]]] = {}  # агент (нижний регистр) → [(разрешено, путь)]
        self._parse(text)

    def _parse(self, text: str) -> None:
        agents: list[str] = []
        in_rules = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            field, value = (part.strip() for part in line.split(":", 1))
            field = field.lower()
            if field == "user-agent":
                if in_rules:  # новая группа начинается после правил прежней
                    agents, in_rules = [], False
                agents.append(value.lower())
                for agent in agents:
                    self.groups.setdefault(agent, [])
            elif field in ("allow", "disallow"):
                in_rules = True
                if not value:
                    continue  # «Disallow:» без пути ничего не запрещает
                for agent in agents:
                    self.groups.setdefault(agent, []).append((field == "allow", value))

    def _rules_for(self, user_agent: str) -> list[tuple[bool, str]]:
        token = user_agent.split("/", 1)[0].strip().lower()
        for agent, rules in self.groups.items():
            if agent != "*" and agent and agent in token:
                return rules
        return self.groups.get("*", [])

    def can_fetch(self, user_agent: str, url: str) -> bool:
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if target == "/robots.txt":
            return True
        best: tuple[int, bool] | None = None  # (длина правила, разрешено)
        for allow, path in self._rules_for(user_agent):
            if _pattern(path).match(target):
                candidate = (len(path), allow)
                if best is None or candidate[0] > best[0] or (candidate[0] == best[0] and allow):
                    best = candidate
        return True if best is None else best[1]
