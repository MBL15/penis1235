"""Общая основа адаптеров источников: вежливая загрузка страниц и разбор дат.

Правила, которые соблюдают все адаптеры (они зашиты в Fetcher, а не оставлены на совесть каждого):
* перед обходом сайта читается его robots.txt; запрещено или не прочитался (кроме «файла нет»): страница не загружается;
* между запросами к одному сайту пауза, запросы идут с честным User-Agent и по HTTPS, ответ не больше лимита размера;
* ходим только на разрешённые адаптером хосты, в том числе после перенаправлений;
* капча, 403 и 429 (сайт просит нас остановиться) прекращают обход: ни капчу, ни блокировку мы не обходим.
"""
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from urllib.robotparser import RobotFileParser

from ..config import LOCAL_TZ, PUBLIC_URL

log = logging.getLogger("afisha.sources")

USER_AGENT = f"bashafisha-hackathon/1.0 (+{PUBLIC_URL})"
MAX_BYTES = 8 * 1024 * 1024


class SourceError(Exception):
    """Источник недоступен, запретил обход или отдал неожиданную разметку."""


class BlockedError(SourceError):
    """Сайт просит остановиться (капча, 403, 429). Обход прекращается, обходить блокировку нельзя."""


class Fetcher:
    """Загрузчик страниц с проверкой robots.txt, паузами, повторами и защитой от лишнего."""

    def __init__(
        self,
        allowed_hosts: tuple[str, ...],
        *,
        delay: float = 1.2,
        retries: int = 3,
        timeout: float = 25.0,
        max_bytes: int = MAX_BYTES,
        user_agent: str = USER_AGENT,
        opener: urllib.request.OpenerDirector | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.allowed_hosts = tuple(h.lower() for h in allowed_hosts)
        self.delay, self.retries, self.timeout, self.max_bytes = delay, retries, timeout, max_bytes
        self.user_agent = user_agent
        # Российские сайты через зарубежный прокси недоступны, поэтому по умолчанию идём напрямую
        # (AFISHA_USE_PROXY=1 включает прокси окружения).
        self.opener = opener or (
            urllib.request.build_opener() if os.getenv("AFISHA_USE_PROXY") == "1" else urllib.request.build_opener(urllib.request.ProxyHandler({}))
        )
        self._sleep, self._clock = sleep, clock
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self.requests = 0

    # ---- проверки ----

    def host_ok(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in self.allowed_hosts)

    def _read(self, url: str) -> tuple[int, str, str]:
        """Один запрос с повторами. Возвращает (код, текст, итоговый адрес). 404/410 отдаются как есть."""
        if not self.host_ok(url):
            raise SourceError(f"адрес вне списка разрешённых хостов: {url}")
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept-Language": "ru", "Accept-Encoding": "identity"})
        last: Exception | None = None
        for attempt in range(self.retries):
            self._pause(url)
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    final = response.geturl()
                    if not self.host_ok(final):
                        raise SourceError(f"{url}: перенаправление на посторонний адрес {final}")
                    body = response.read(self.max_bytes + 1)
                    if len(body) > self.max_bytes:
                        raise SourceError(f"{url}: ответ больше {self.max_bytes // 1024 // 1024} МБ")
                    self.requests += 1
                    return response.status, body.decode("utf-8", errors="replace"), final
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429):
                    raise BlockedError(f"{url}: сайт ответил {exc.code}, обход остановлен") from exc
                if exc.code in (404, 410):
                    return exc.code, "", url
                last = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
            if attempt + 1 < self.retries:
                self._sleep(min(2 * (attempt + 1), 8))
        raise SourceError(f"{url}: {last}")

    def _pause(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).hostname or ""
        wait = self._last_hit.get(host, 0) + self.delay - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._last_hit[host] = self._clock()

    def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            status, text, _ = self._read(origin + "/robots.txt")
            if status in (404, 410):
                self._robots[origin] = None  # файла нет: ограничений нет
            else:
                parser = RobotFileParser()
                parser.parse(text.splitlines())
                self._robots[origin] = parser
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        parser = self._robots_for(url)
        return parser is None or parser.can_fetch(self.user_agent, url)

    # ---- публичный метод ----

    def get(self, url: str, *, robots: bool = True) -> str:
        """Текст страницы. robots=False только для документированного открытого API (robots.txt относится к обходу страниц)."""
        if robots and not self.allowed(url):
            raise SourceError(f"robots.txt запрещает обход {url}")
        status, text, final = self._read(url)
        if status in (404, 410):
            raise SourceError(f"{url}: страницы нет ({status})")
        if re.search(r"captcha|showcaptcha", urllib.parse.urlsplit(final).path + urllib.parse.urlsplit(final).query, re.I) or (
            len(text) < 30_000 and re.search(r"smartcaptcha|are you a robot|вы не робот", text, re.I)
        ):
            raise BlockedError(f"{url}: сайт показал капчу, обход остановлен")
        return text


# ---------- даты ----------

_ISO_TZ = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


def parse_datetime(value, *, tz_mode: str = "honor") -> datetime | None:
    """ISO-дата из разметки → местное время Уфы без tzinfo.

    tz_mode='honor': смещение учитывается (нет смещения: время уже местное).
    tz_mode='local': смещение игнорируется, берётся «как написано на стене». Нужно сайтам, которые пишут местное время
    с ложной меткой UTC (kassir.ru: на странице «начало в 13:00», а в разметке «13:00+00:00»).
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        if tz_mode == "local":
            parsed = datetime.fromisoformat(_ISO_TZ.sub("", text.replace(" ", "T")))
            return parsed.replace(tzinfo=None)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return parsed
