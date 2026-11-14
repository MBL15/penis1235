"""Защита агента (ТЗ §6.5, §7.3, метрика 7): инструкции внутри текста не выполняются, служебные указания не раскрываются,
посторонние вопросы получают короткий ответ.

* detect_injection: попытка заставить агента нарушить правила или раскрыть системный промпт.
* neutralize: из описаний событий (данные из внешних источников) вырезаются фразы, похожие на инструкции, прежде чем они
  попадут в модель. Описание остаётся данными, а не командой.
* canary: случайная метка в системном промпте; если она появилась в ответе, промпт утёк и ответ заменяется отказом.
* is_offtopic: вопрос не про афишу, отвечаем коротко и возвращаем к задаче без обращения к модели."""
import re
import secrets

from .normalize import norm_text

_PATTERNS = (
    r"игнорир\w*\s+(?:\w+\s+){0,3}(?:инструкц|правил|указани|промпт|ограничен)",
    r"забуд\w*\s+(?:\w+\s+){0,3}(?:инструкц|правил|указани|промпт|ограничен)",
    r"не\s+обращай\s+внимани\w*\s+(?:\w+\s+){0,3}(?:инструкц|правил|указани)",
    r"(?:покаж\w*|выведи|раскро\w*|напиши|повтори|пришли|дай|назови|перечисли)\s+(?:\w+\s+){0,3}(?:системн\w*\s+(?:промпт|инструкц|сообщени)|свои\s+(?:инструкц|правил)|промпт|prompt)",
    r"системн\w*\s+(?:промпт|инструкц)",
    r"(?:твои|ваши|свои)\s+(?:служебн\w*|скрыт\w*|внутренн\w*)\s+(?:инструкц|указани|правил)",
    r"ignore\s+(?:\w+\s+){0,3}(?:instruction|rule|prompt|polic)",
    r"(?:reveal|show|print|repeat|output)\s+(?:\w+\s+){0,3}(?:system\s+prompt|instruction|your\s+prompt)",
    r"system\s+prompt",
    r"ты\s+теперь\s+\w+",
    r"you\s+are\s+now",
    r"(?:новые|другие|дополнительные)\s+инструкции",
    r"опубликуй\s+(?:\w+\s+){0,2}(?:текст|сообщение)",
    r"режим\s+разработчика|developer\s+mode|jailbreak|dan\s+mode",
)
INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.I)
HIDDEN = "[фрагмент скрыт: похож на инструкцию]"
_SENTENCE = re.compile(r"(?<=[.!?…\n;])\s+|\n+")

REFUSAL = (
    "Служебные инструкции я не раскрываю и не меняю. Могу подобрать события в афише: напишите, когда, где и что вас интересует, "
    "например «в субботу с ребёнком до 1000 рублей»."
)
OFFTOPIC_REPLY = (
    "Я помогаю только с афишей культурных событий Башкортостана: концерты, спектакли, выставки, экскурсии. "
    "Напишите, куда хотите сходить, например «бесплатные выставки в Уфе»."
)

EVENT_WORDS = (
    "концерт", "спектакл", "выставк", "фестивал", "кино", "фильм", "лекци", "экскурс", "музе", "театр", "балет", "опер",
    "мероприят", "событи", "афиш", "билет", "сходить", "сходим", "пойти", "куда", "посмотреть", "послушать", "погулять",
    "заняться", "развлеч", "отдохнуть", "выходн", "детям", "ребен", "мастер", "джаз", "стендап", "шоу", "матч", "спорт",
    "ярмарк", "праздник", "посоветуй",
)
OFFTOPIC_PHRASES = (
    "погод", "курс валют", "курс доллар", "курс евро", "рецепт", "анекдот", "гороскоп", "биткоин", "переведи ", "напиши код",
    "сочини", "кто ты", "как дела", "расскажи о себе", "расскажи про себя", "сколько будет", "реши ", "стихотворени",
    "расскажи шутку", "президент",
)


def detect_injection(text: str | None) -> bool:
    """Просит ли текст нарушить правила, сменить роль или раскрыть служебные инструкции."""
    return bool(INJECTION_RE.search((text or "").replace("ё", "е")))


def neutralize(text: str | None) -> tuple[str, bool]:
    """(текст без фраз-инструкций, были ли они). Описание события — данные: приказы из него модель не получает."""
    if not text or not detect_injection(text):
        return text or "", False
    kept = [HIDDEN if detect_injection(part) else part for part in _SENTENCE.split(text) if part.strip()]
    deduped = [part for i, part in enumerate(kept) if not (part == HIDDEN and i and kept[i - 1] == HIDDEN)]
    return " ".join(deduped), True


def new_canary() -> str:
    return "CANARY-" + secrets.token_hex(6)


def leaks_prompt(reply: str | None, canary: str) -> bool:
    return bool(reply) and canary in reply


_NUMBER = re.compile(r"\d+(?::\d{2})?")


def ungrounded_numbers(reply: str | None, source_text: str) -> list[str]:
    """Числа и время из ответа (две цифры и больше), которых нет ни в результатах инструментов, ни в самом вопросе.

    Грубая проверка «каждый факт взят из данных» (ТЗ §7.1, метрика 1): цены, даты, время и возраст в ответе должны
    встречаться в том, что вернули инструменты. Ответ с такими числами пользователю не показывается (agent._checked)."""
    missing = []
    for token in dict.fromkeys(_NUMBER.findall(reply or "")):
        if len(token) < 2:
            continue
        if not re.search(rf"(?<![\d:]){re.escape(token)}(?![\d:])", source_text):
            missing.append(token)
    return missing


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?:\+7|(?<![\d+])8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")


def mask_pii(text: str | None) -> str:
    """Телефоны и почта из текста трассы (ТЗ §10: персональные данные минимизированы). Трасса открыта по ссылке 30 дней,
    а человек мог написать в вопросе свой номер. Цены, даты и время такой шаблон не задевает."""
    return _PHONE.sub("[телефон скрыт]", _EMAIL.sub("[почта скрыта]", text or ""))


_QUOTED = re.compile(r"«([^»]{3,120})»")
_ECHO = re.compile(r"\([^)]*\)|слов\w*\s+«[^»]*»")  # пересказ условий в скобках и «со словом «…»»: это слова запроса


def ungrounded_titles(reply: str | None, source_text: str, allowed_text: str = "") -> list[str]:
    """Названия в «ёлочках», которых нет ни в результатах инструментов, ни в вопросе, ни в готовых фразах сервиса
    (allowed_text: системный промпт с примерами вроде «в источнике нет описания»). Признак выдуманного события (ТЗ §7.1)."""
    known = f" {norm_text(source_text)} {norm_text(allowed_text)} "
    missing = []
    for quoted in dict.fromkeys(_QUOTED.findall(_ECHO.sub(" ", reply or ""))):
        key = norm_text(quoted)
        if key and f" {key} " not in known:
            missing.append(quoted)
    return missing


def has_event_signal(text: str | None) -> bool:
    words = norm_text(text or "").split()
    return any(word.startswith(stem) for word in words for stem in EVENT_WORDS)


def is_offtopic(text: str | None) -> bool:
    low = norm_text(text or "") + " "
    return any(phrase in low for phrase in OFFTOPIC_PHRASES) and not has_event_signal(text)
