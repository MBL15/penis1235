"""Telegram-бот афиши. Запуск: python -m app.bot (нужен TELEGRAM_BOT_TOKEN).

Бот работает на тех же данных и том же поиске, что и сайт с API: кнопки быстрых фильтров плюс свободный текст через ИИ-агента.
"""
import asyncio
import html
import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message, WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.orm import Session

from . import config
from .agent import ask
from .config import now_local
from .db import SessionLocal, init_db
from .formatting import render_event, render_list
from .models import Event
from .normalize import CATEGORIES
from .search import SearchFilters, facets, get_event, resolve_when, search
from .security import RateLimiter

log = logging.getLogger("afisha.bot")
router = Router()

PAGE = 5
MAX_TEXT = 4000  # лимит Telegram — 4096 символов
MAX_QUESTION = 1000  # столько же принимает /api/ask: длинное сообщение не должно раздувать запрос к модели
MAX_CHATS = 5000  # сколько чатов помним в памяти процесса; самые давние забываем

QUICK = {
    "today": "Сегодня",
    "tomorrow": "Завтра",
    "weekend": "На выходных",
    "week": "На неделе",
    "free": "Бесплатно",
    "kids": "Детям",
}

# Состояние чатов хранится в памяти процесса: для демо этого достаточно.
chat_city: dict[int, str] = {}
chat_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=10))
# Свободный текст уходит в платную модель: не больше 8 вопросов в минуту с одного пользователя и не больше одного
# одновременно (иначе один человек занимает все потоки и квоту).
ai_limiter = RateLimiter(limit=8, detail="Слишком много вопросов подряд. Подождите минуту и спросите ещё раз.")
ai_busy: set[int] = set()


def _forget_oldest(store: dict, limit: int = MAX_CHATS) -> None:
    """Словари состояния живут в памяти процесса: не даём им расти без предела."""
    while len(store) > limit:
        store.pop(next(iter(store)))

WELCOME = (
    "👋 <b>Афиша культуры Башкортостана</b>\n"
    "Реальные события республики: без дублей, со статусами отмен и переносов.\n\n"
    "Нажмите кнопку или просто напишите, что ищете, например:\n"
    "<i>«куда пойти в субботу с ребёнком до 1000 рублей»</i>\n\n"
    "Город: /city · Категории: /categories"
)


@dataclass
class View:
    text: str
    markup: InlineKeyboardMarkup


# ---------- обязательная подписка на канал ----------

SUBSCRIBED = {"creator", "administrator", "member"}
SUB_TTL = 600  # подтверждённую подписку не перепроверяем 10 минут: меньше запросов к Telegram
_subscribed_until: dict[int, float] = {}


def channel_url() -> str:
    return f"https://t.me/{config.REQUIRED_CHANNEL.lstrip('@')}"


def subscribe_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📣 Подписаться на {config.REQUIRED_CHANNEL_TITLE}", url=channel_url())],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="sub:check")],
    ])


def subscribe_text() -> str:
    return (
        f"🔒 Чтобы пользоваться афишей, подпишитесь на канал <b>{html.escape(config.REQUIRED_CHANNEL_TITLE)}</b> "
        f"({html.escape(config.REQUIRED_CHANNEL)}).\n\nПосле подписки нажмите «Я подписался»."
    )


async def is_subscribed(bot: Bot, user_id: int, *, fresh: bool = False) -> bool:
    """Подписан ли пользователь на обязательный канал. Не удалось проверить: считаем, что не подписан
    (условие обязательное), а причину пишем в лог: чаще всего бот не назначен администратором канала."""
    if not config.REQUIRED_CHANNEL:
        return True
    if not fresh and _subscribed_until.get(user_id, 0) > time.monotonic():
        return True
    try:
        member = await bot.get_chat_member(config.REQUIRED_CHANNEL, user_id)
    except TelegramAPIError as exc:
        log.error("Не удалось проверить подписку на %s: %s. Сделайте бота администратором канала.",
                  config.REQUIRED_CHANNEL, exc)
        return False
    status = getattr(member.status, "value", member.status)
    ok = status in SUBSCRIBED or (status == "restricted" and bool(getattr(member, "is_member", False)))
    if ok:
        _subscribed_until[user_id] = time.monotonic() + SUB_TTL
        _forget_oldest(_subscribed_until, 50_000)
    else:
        _subscribed_until.pop(user_id, None)
    return ok


class SubscriptionGate(BaseMiddleware):
    """Пропускает к обработчикам только подписчиков канала; остальным показывает приглашение подписаться."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not config.REQUIRED_CHANNEL:
            return await handler(event, data)
        if user is None or user.is_bot:
            # Сообщение от имени канала или анонимного администратора: проверить подписку не у кого, значит не пускаем.
            return None
        bot: Bot = data["bot"]

        if isinstance(event, CallbackQuery) and event.data == "sub:check":
            if await is_subscribed(bot, user.id, fresh=True):
                await event.answer("Подписка подтверждена ✅")
                if event.message is not None:
                    chat_id = event.message.chat.id
                    await event.message.answer(WELCOME, reply_markup=menu_markup(chat_city.get(chat_id)))
            else:
                await event.answer("Подписки пока нет. Подпишитесь на канал и нажмите кнопку ещё раз.", show_alert=True)
            return None

        if await is_subscribed(bot, user.id):
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer()
            if event.message is not None:
                await event.message.answer(subscribe_text(), reply_markup=subscribe_markup(), disable_web_page_preview=True)
        else:
            await event.answer(subscribe_text(), reply_markup=subscribe_markup(), disable_web_page_preview=True)
        return None


class LiveMessageOnly(BaseMiddleware):
    """Кнопка под сообщением, которого у бота уже нет (слишком старое или удалено): обработчикам некуда отвечать."""

    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery) and not isinstance(event.message, Message):
            await event.answer("Сообщение устарело. Откройте меню: /start", show_alert=True)
            return None
        return await handler(event, data)


router.message.outer_middleware(SubscriptionGate())
router.callback_query.outer_middleware(SubscriptionGate())
router.callback_query.middleware(LiveMessageOnly())


# ---------- чистая логика (тестируется без Telegram) ----------

def quick_filters(kind: str, city: str | None, offset: int = 0) -> SearchFilters:
    today = now_local().date()
    f = SearchFilters(city=city, limit=PAGE, offset=offset)
    if kind in ("today", "tomorrow", "weekend", "week"):
        f.date_from, f.date_to = resolve_when(kind, today)
    elif kind == "free":
        f.free_only = True
    elif kind == "kids":
        f.kids = True
    elif kind.startswith("cat_"):
        f.category = kind[4:]
    else:
        raise ValueError(f"неизвестный фильтр {kind}")
    return f


def menu_markup(city: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for kind, label in QUICK.items():
        kb.button(text=label, callback_data=f"l:{kind}:0")
    kb.button(text=f"📍 {city or 'Вся республика'}", callback_data="cities")
    kb.button(text="🗂 Категории", callback_data="cats")
    kb.adjust(3, 3, 2)
    markup = kb.as_markup()
    if config.WEBAPP_URL:  # главная кнопка: открыть афишу внутри Telegram
        markup.inline_keyboard.insert(0, [InlineKeyboardButton(text="🗓 Открыть афишу", web_app=WebAppInfo(url=config.WEBAPP_URL))])
    return markup


def clip(text: str) -> str:
    """Влезает в сообщение Telegram и не рвёт HTML: режем по границе строки (теги не пересекают строки),
    а если строки нет — не оставляем половину сущности вроде «&am»."""
    if len(text) <= MAX_TEXT:
        return text
    cut = text[:MAX_TEXT]
    newline = cut.rfind("\n")
    if newline > MAX_TEXT // 2:
        return cut[:newline]
    amp = cut.rfind("&")
    return cut[:amp] if amp > cut.rfind(";") else cut


def list_view(session: Session, kind: str, city: str | None, offset: int = 0) -> View:
    events, total = search(session, quick_filters(kind, city, offset))
    title = QUICK.get(kind) or CATEGORIES.get(kind.removeprefix("cat_"), kind)
    where = f" · {city}" if city else ""
    if not events:
        return View(f"<b>{html.escape(title)}{html.escape(where)}</b>\nНичего не нашлось. Попробуйте другой фильтр или город.",
                    menu_markup(city))
    head = f"<b>{html.escape(title)}{html.escape(where)}</b> · найдено {total}\n\n"
    return View(clip(head + render_list(events, html=True)), list_markup(events, kind, offset, total, city))


def list_markup(events: list[Event], kind: str, offset: int, total: int, city: str | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for number, event in enumerate(events, 1):
        kb.button(text=f"{number}. {event.title[:28]}", callback_data=f"e:{event.id}")
    kb.adjust(1)
    nav = InlineKeyboardBuilder()
    if offset + len(events) < total:
        nav.button(text="Ещё ▸", callback_data=f"l:{kind}:{offset + PAGE}")
    nav.button(text="🏠 Меню", callback_data="menu")
    kb.attach(nav)
    return kb.as_markup()


def event_view(session: Session, event_id: int) -> View | None:
    event = get_event(session, event_id)
    if event is None:
        return None
    kb = InlineKeyboardBuilder()
    if (event.url or "").startswith(("http://", "https://")):
        kb.button(text="Открыть на сайте", url=event.url)
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    return View(clip(render_event(event, html=True, details=True)), kb.as_markup())


def cities_markup(session: Session) -> tuple[InlineKeyboardMarkup, list[str]]:
    names = [c["name"] for c in facets(session)["cities"]]
    kb = InlineKeyboardBuilder()
    kb.button(text="Вся республика", callback_data="city:-1")
    for name in names:
        # В callback_data — название, а не номер в списке: порядок городов меняется вместе с числом событий.
        if len(f"city:{name}".encode()) <= 64:  # лимит Telegram; слишком длинное название сломало бы всю клавиатуру
            kb.button(text=name, callback_data=f"city:{name}")
    kb.adjust(2)
    return kb.as_markup(), names


def categories_markup(session: Session) -> InlineKeyboardMarkup:
    counts = {c["slug"]: c["count"] for c in facets(session)["categories"]}
    kb = InlineKeyboardBuilder()
    for slug, label in CATEGORIES.items():
        if counts.get(slug):
            kb.button(text=f"{label} · {counts[slug]}", callback_data=f"l:cat_{slug}:0")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(2)
    return kb.as_markup()


def agent_view(chat_id: int, text: str) -> View:
    """Свободный текст → ИИ-агент (или правила). Блокирующий вызов: запускать в потоке."""
    city = chat_city.get(chat_id)
    message = f"{text}\n(Город пользователя: {city})" if city else text
    with SessionLocal() as session:
        result = ask(session, message, list(chat_history[chat_id]))
        chat_history[chat_id].extend([{"role": "user", "content": text}, {"role": "assistant", "content": result.reply}])
        _forget_oldest(chat_history)
        body = html.escape(result.reply)
        # Развёрнутый ответ Gemini уже описывает каждое событие; короткий ответ правил дополняем списком карточек.
        if result.events and result.provider != "gemini":
            body += "\n\n" + render_list(result.events, html=True)
        kb = InlineKeyboardBuilder()
        for number, event in enumerate(result.events, 1):
            kb.button(text=f"{number}. {event.title[:28]}", callback_data=f"e:{event.id}")
        kb.button(text="🏠 Меню", callback_data="menu")
        kb.adjust(1)
        return View(clip(body), kb.as_markup())


# ---------- обработчики ----------

@router.message(CommandStart())
@router.message(Command("help"))
async def on_start(message: Message) -> None:
    await message.answer(WELCOME, reply_markup=menu_markup(chat_city.get(message.chat.id)))


@router.message(Command(*QUICK))
async def on_quick_command(message: Message) -> None:
    kind = (message.text or "").lstrip("/").split("@")[0].split()[0]
    with SessionLocal() as session:
        view = list_view(session, kind, chat_city.get(message.chat.id))
    await message.answer(view.text, reply_markup=view.markup, disable_web_page_preview=True)


@router.message(Command("app"))
async def on_app(message: Message) -> None:
    if not config.WEBAPP_URL:
        await message.answer("Мини-приложение пока не подключено: не задан HTTPS-адрес (WEBAPP_URL).")
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🗓 Открыть афишу", web_app=WebAppInfo(url=config.WEBAPP_URL))]]
    )
    await message.answer("Вся афиша с фильтрами и ИИ-подбором в одном окне:", reply_markup=markup)


@router.message(Command("city"))
async def on_city(message: Message) -> None:
    with SessionLocal() as session:
        markup, _ = cities_markup(session)
    await message.answer("Выберите город:", reply_markup=markup)


@router.message(Command("categories"))
async def on_categories(message: Message) -> None:
    with SessionLocal() as session:
        markup = categories_markup(session)
    await message.answer("Выберите категорию:", reply_markup=markup)


@router.callback_query(F.data == "menu")
async def on_menu(call: CallbackQuery) -> None:
    await call.message.answer("Что показать?", reply_markup=menu_markup(chat_city.get(call.message.chat.id)))
    await call.answer()


@router.callback_query(F.data == "cities")
async def on_cities(call: CallbackQuery) -> None:
    with SessionLocal() as session:
        markup, _ = cities_markup(session)
    await call.message.answer("Выберите город:", reply_markup=markup)
    await call.answer()


@router.callback_query(F.data == "cats")
async def on_cats(call: CallbackQuery) -> None:
    with SessionLocal() as session:
        markup = categories_markup(session)
    await call.message.answer("Выберите категорию:", reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("city:"))
async def on_pick_city(call: CallbackQuery) -> None:
    name = call.data.removeprefix("city:")
    chat_id = call.message.chat.id
    with SessionLocal() as session:
        _, names = cities_markup(session)
    if name in names:
        chat_city[chat_id] = name
        _forget_oldest(chat_city)
        # Название пришло из источника данных, а сообщение уходит как HTML: экранируем.
        text = f"Город: {html.escape(name)}. Теперь показываю события только здесь."
    elif name == "-1":
        chat_city.pop(chat_id, None)
        text = "Показываю события всей республики."
    else:  # город мог пропасть из афиши, пока открыта старая клавиатура
        text = "Такого города сейчас нет в афише. Выберите другой: /city"
    await call.message.answer(text, reply_markup=menu_markup(chat_city.get(chat_id)))
    await call.answer()


STALE = "Кнопка устарела. Откройте меню: /start"


@router.callback_query(F.data.startswith("l:"))
async def on_list(call: CallbackQuery) -> None:
    # Данные кнопки приходят от клиента и могут быть подделаны: разбираем строго.
    match = re.fullmatch(r"l:([a-z_]{1,40}):([0-9]{1,6})", call.data or "")
    kind = match.group(1) if match else ""
    if not match or not (kind in QUICK or kind.removeprefix("cat_") in CATEGORIES):
        await call.answer(STALE, show_alert=True)
        return
    with SessionLocal() as session:
        view = list_view(session, kind, chat_city.get(call.message.chat.id), int(match.group(2)))
    await call.message.answer(view.text, reply_markup=view.markup, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data.startswith("e:"))
async def on_event(call: CallbackQuery) -> None:
    match = re.fullmatch(r"e:([0-9]{1,9})", call.data or "")
    if not match:
        await call.answer(STALE, show_alert=True)
        return
    with SessionLocal() as session:
        view = event_view(session, int(match.group(1)))
    if view is None:
        await call.answer("Событие больше недоступно", show_alert=True)
        return
    await call.message.answer(view.text, reply_markup=view.markup, disable_web_page_preview=True)
    await call.answer()


@router.message(F.text)
async def on_text(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else message.chat.id
    if user_id in ai_busy:
        await message.answer("Ещё отвечаю на прошлый вопрос. Дождитесь ответа и спросите снова.")
        return
    if not ai_limiter.allow(str(user_id)):
        await message.answer(ai_limiter.detail)
        return
    ai_busy.add(user_id)
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
        view = await asyncio.to_thread(agent_view, message.chat.id, message.text[:MAX_QUESTION])
    except Exception:  # бот не должен падать из-за одного плохого запроса
        log.exception("Ошибка при обработке запроса")
        await message.answer("Что-то пошло не так. Попробуйте переформулировать запрос или нажмите /start.")
        return
    finally:
        ai_busy.discard(user_id)
    await message.answer(view.text, reply_markup=view.markup, disable_web_page_preview=True)


# ---------- запуск ----------

async def run() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN (получите токен у @BotFather и добавьте в .env)")
    init_db()
    session = AiohttpSession(proxy=config.TELEGRAM_PROXY) if config.TELEGRAM_PROXY else None
    bot = Bot(config.TELEGRAM_BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands(
        [BotCommand(command="start", description="Меню"),
         BotCommand(command="app", description="Открыть афишу"),
         BotCommand(command="today", description="Сегодня"),
         BotCommand(command="weekend", description="На выходных"),
         BotCommand(command="free", description="Бесплатно"),
         BotCommand(command="kids", description="Детям"),
         BotCommand(command="city", description="Выбрать город"),
         BotCommand(command="categories", description="Категории")]
    )
    if config.WEBAPP_URL:
        try:  # кнопка «Афиша» слева от поля ввода открывает мини-приложение
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Афиша", web_app=WebAppInfo(url=config.WEBAPP_URL))
            )
        except Exception:
            log.exception("Не удалось задать кнопку меню мини-приложения")
    if config.REQUIRED_CHANNEL:
        try:  # бот должен быть администратором канала, иначе список подписчиков недоступен и пускать некого
            await bot.get_chat_member(config.REQUIRED_CHANNEL, bot.id)
            log.info("Обязательная подписка на %s: проверка доступна", config.REQUIRED_CHANNEL)
        except TelegramAPIError as exc:
            log.error("ПОДПИСКА НА %s НЕ ПРОВЕРИТЬ (%s): назначьте бота администратором канала, "
                      "иначе бот никого не пропустит", config.REQUIRED_CHANNEL, exc)
    log.info("Бот запущен (мини-приложение: %s)", config.WEBAPP_URL or "не подключено")
    await dp.start_polling(bot)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
