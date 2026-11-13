"""Бот целиком: настоящий Dispatcher aiogram, но без сети (отправка в Telegram подменена)."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app import bot, config, subscriptions
from .demo_data import seed

from .test_formatting_and_bot import _Ctx

TOKEN = "123456:" + "A" * 35
CHAT = 7


def make_message(text: str) -> Message:
    return Message(message_id=1, date=datetime.now(), chat=Chat(id=CHAT, type="private"),
                   from_user=User(id=CHAT, is_bot=False, first_name="Тест"), text=text)


@pytest.fixture(scope="module")
def dispatcher():
    dp = Dispatcher()
    dp.include_router(bot.router)  # глобальный router можно подключить только один раз
    return dp


@pytest.fixture
def wired(session, monkeypatch):
    seed(session)
    monkeypatch.setattr(bot, "SessionLocal", lambda: _Ctx(session))
    answer, cb_answer, typing = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(CallbackQuery, "answer", cb_answer)
    monkeypatch.setattr(Bot, "send_chat_action", typing)
    bot.chat_city.clear()
    bot.chat_history.clear()
    bot.sub_draft.clear()
    bot.ai_limiter.hits.clear()
    bot.ai_busy.clear()
    return SimpleBot(answer, cb_answer)


class SimpleBot:
    def __init__(self, answer, cb_answer):
        self.answer, self.cb_answer = answer, cb_answer
        self.bot = Bot(TOKEN)

    def send(self, dispatcher, **fields):
        return asyncio.run(dispatcher.feed_update(self.bot, Update(update_id=1, **fields)))

    def say(self, dispatcher, text):
        return self.send(dispatcher, message=make_message(text))

    def press(self, dispatcher, data):
        call = CallbackQuery(id="1", from_user=User(id=CHAT, is_bot=False, first_name="Тест"), chat_instance="x",
                             data=data, message=make_message("меню"))
        return self.send(dispatcher, callback_query=call)

    @property
    def last_text(self) -> str:
        return self.answer.call_args.args[0]

    @property
    def last_markup(self):
        return self.answer.call_args.kwargs["reply_markup"]


def test_start_shows_menu(dispatcher, wired):
    wired.say(dispatcher, "/start")
    assert "Афиша культуры Башкортостана" in wired.last_text
    assert wired.last_markup.inline_keyboard


def test_quick_command_lists_events(dispatcher, wired):
    wired.say(dispatcher, "/free")
    assert "Бесплатно" in wired.last_text and "найдено" in wired.last_text
    callbacks = [b.callback_data for row in wired.last_markup.inline_keyboard for b in row]
    assert any(c.startswith("e:") for c in callbacks)


def test_pressing_event_button_shows_the_card(dispatcher, wired):
    wired.say(dispatcher, "/free")
    callbacks = [b.callback_data for row in wired.last_markup.inline_keyboard for b in row]
    wired.press(dispatcher, next(c for c in callbacks if c.startswith("e:")))
    assert "Источники:" in wired.last_text
    wired.cb_answer.assert_awaited()


def test_city_choice_limits_later_lists(dispatcher, wired, session):
    wired.press(dispatcher, "city:Салават")
    assert bot.chat_city[CHAT] == "Салават"
    wired.say(dispatcher, "/week")
    assert "Салават" in wired.last_text
    assert "Уфа" not in wired.last_text.replace("Салават", "")  # только события выбранного города
    wired.press(dispatcher, "city:-1")
    assert CHAT not in bot.chat_city


def test_free_text_goes_to_the_agent_and_keeps_history(dispatcher, wired):
    wired.say(dispatcher, "бесплатно на выходных")
    assert "Нашёл" in wired.last_text or "ничего" in wired.last_text
    assert [m["role"] for m in bot.chat_history[CHAT]] == ["user", "assistant"]
    wired.say(dispatcher, "а ещё что-нибудь?")  # расплывчатый вопрос продолжает прежний диалог
    assert "бесплатно" in wired.last_text

    bot.chat_history.clear()  # в новом диалоге тот же вопрос слишком общий → бот уточняет
    wired.say(dispatcher, "что посоветуешь?")
    assert "Подскажите" in wired.last_text


def test_unknown_callback_event_is_reported(dispatcher, wired):
    wired.press(dispatcher, "e:999999")
    wired.cb_answer.assert_awaited_with("Событие больше недоступно", show_alert=True)


# ---------- обязательная подписка на канал ----------

@pytest.fixture
def gate(monkeypatch):
    """Включает проверку подписки; get_chat_member возвращает статус из gate.status (или бросает ошибку)."""
    monkeypatch.setattr(config, "REQUIRED_CHANNEL", "@sz_orenteam")
    monkeypatch.setattr(config, "REQUIRED_CHANNEL_TITLE", "SpaceZ")
    bot._subscribed_until.clear()
    state = SimpleNamespace(status="left", calls=AsyncMock())

    async def get_chat_member(self, chat_id, user_id):
        await state.calls(chat_id, user_id)
        if state.status == "error":
            raise TelegramBadRequest(method=None, message="Bad Request: member list is inaccessible")
        return SimpleNamespace(status=state.status, is_member=getattr(state, "is_member", False))

    monkeypatch.setattr(Bot, "get_chat_member", get_chat_member)
    yield state
    bot._subscribed_until.clear()


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_non_subscriber_gets_the_invitation_and_no_menu(dispatcher, wired, gate):
    wired.say(dispatcher, "/start")
    assert "подпишитесь" in wired.last_text and "SpaceZ" in wired.last_text and "@sz_orenteam" in wired.last_text
    found = buttons(wired.last_markup)
    assert any(b.url == "https://t.me/sz_orenteam" for b in found)
    assert any(b.callback_data == "sub:check" for b in found)
    assert "Афиша культуры Башкортостана" not in wired.last_text  # обработчик /start не выполнялся

    wired.say(dispatcher, "бесплатно на выходных")  # свободный текст тоже закрыт
    assert "подпишитесь" in wired.last_text and not bot.chat_history[CHAT]

    wired.answer.reset_mock()
    wired.press(dispatcher, "e:1")  # и нажатие на старую кнопку
    assert "подпишитесь" in wired.last_text
    wired.cb_answer.assert_awaited()


@pytest.mark.parametrize("status", ["member", "administrator", "creator"])
def test_subscriber_is_let_through(dispatcher, wired, gate, status):
    gate.status = status
    wired.say(dispatcher, "/start")
    assert "Афиша культуры Башкортостана" in wired.last_text


def test_restricted_member_counts_only_while_still_in_the_channel(dispatcher, wired, gate):
    gate.status, gate.is_member = "restricted", True
    wired.say(dispatcher, "/start")
    assert "Афиша культуры Башкортостана" in wired.last_text
    bot._subscribed_until.clear()
    gate.is_member = False
    wired.say(dispatcher, "/start")
    assert "подпишитесь" in wired.last_text


def test_confirmed_subscription_is_cached_and_check_button_rechecks(dispatcher, wired, gate):
    gate.status = "member"
    wired.say(dispatcher, "/free")
    wired.say(dispatcher, "/week")
    assert gate.calls.await_count == 1  # вторая команда обошлась без запроса к Telegram

    gate.status = "left"  # отписался, но кэш ещё держится: пускаем
    wired.say(dispatcher, "/today")
    assert "найдено" in wired.last_text or "Ничего" in wired.last_text
    wired.press(dispatcher, "sub:check")  # а кнопка «Я подписался» всегда проверяет заново
    wired.cb_answer.assert_awaited_with("Подписки пока нет. Подпишитесь на канал и нажмите кнопку ещё раз.", show_alert=True)


def test_check_button_opens_the_menu_after_subscribing(dispatcher, wired, gate):
    wired.press(dispatcher, "sub:check")
    assert wired.cb_answer.await_args.kwargs.get("show_alert") is True and not wired.answer.await_count
    gate.status = "member"
    wired.press(dispatcher, "sub:check")
    assert "Афиша культуры Башкортостана" in wired.last_text and wired.last_markup.inline_keyboard


def test_unverifiable_subscription_is_not_a_free_pass(dispatcher, wired, gate):
    gate.status = "error"  # бот не администратор канала: Telegram не отдаёт список подписчиков
    wired.say(dispatcher, "/start")
    assert "подпишитесь" in wired.last_text


def test_gate_can_be_switched_off(dispatcher, wired, gate, monkeypatch):
    monkeypatch.setattr(config, "REQUIRED_CHANNEL", "")
    wired.say(dispatcher, "/start")
    assert "Афиша культуры Башкортостана" in wired.last_text and not gate.calls.await_count


# ---------- защита бота ----------

def test_city_name_from_a_source_is_escaped(dispatcher, wired, add):
    add("Концерт", datetime.now().replace(microsecond=0) + timedelta(days=2), city="Нью & <Йорк>")
    wired.press(dispatcher, "city:Нью & <Йорк>")
    assert "Город: Нью &amp; &lt;Йорк&gt;." in wired.last_text


@pytest.mark.parametrize("data", ["l:today", "l:today:abc", "l:today:-5", "l:drop table:0", "l:cat_nope:0", "l:today:0:9",
                                  "e:", "e:abc", "e:1;drop", "e:" + "9" * 30])
def test_forged_button_data_is_refused_politely(dispatcher, wired, data):
    wired.press(dispatcher, data)
    wired.cb_answer.assert_awaited_with(bot.STALE, show_alert=True)
    assert not wired.answer.await_count


def test_button_under_a_vanished_message_does_not_crash(dispatcher, wired):
    call = CallbackQuery(id="1", from_user=User(id=CHAT, is_bot=False, first_name="Тест"), chat_instance="x", data="menu")
    wired.send(dispatcher, callback_query=call)
    assert wired.cb_answer.await_args.kwargs.get("show_alert") is True and not wired.answer.await_count


def test_free_text_is_rate_limited_per_user(dispatcher, wired, monkeypatch):
    monkeypatch.setattr(bot.ai_limiter, "limit", 2)
    for _ in range(2):
        wired.say(dispatcher, "бесплатно на выходных")
    wired.say(dispatcher, "а ещё?")
    assert wired.last_text == bot.ai_limiter.detail
    assert len(bot.chat_history[CHAT]) == 4  # третий вопрос до агента не дошёл
    wired.say(dispatcher, "/free")  # кнопки и команды лимит не трогает
    assert "Бесплатно" in wired.last_text


def test_second_question_waits_for_the_first(dispatcher, wired):
    bot.ai_busy.add(CHAT)
    wired.say(dispatcher, "бесплатно на выходных")
    assert "Ещё отвечаю" in wired.last_text and not bot.chat_history[CHAT]
    bot.ai_busy.clear()
    wired.say(dispatcher, "бесплатно на выходных")
    assert CHAT not in bot.ai_busy and bot.chat_history[CHAT]  # после ответа отметка снята


def test_long_message_is_cut_before_the_agent(dispatcher, wired, monkeypatch):
    seen = []
    monkeypatch.setattr(bot, "agent_view", lambda chat_id, text: seen.append(text) or bot.View("ок", bot.menu_markup()))
    wired.say(dispatcher, "я" * 4000)
    assert len(seen[0]) == bot.MAX_QUESTION


def test_message_without_a_user_does_not_pass_the_gate(dispatcher, wired, gate):
    gate.status = "member"
    anonymous = Message(message_id=1, date=datetime.now(), chat=Chat(id=-100, type="supergroup"),
                        sender_chat=Chat(id=-100, type="supergroup"), text="/start")
    wired.send(dispatcher, message=anonymous)
    assert not wired.answer.await_count and not gate.calls.await_count


def test_state_dictionaries_do_not_grow_forever():
    store = {i: i for i in range(10)}
    bot._forget_oldest(store, limit=4)
    assert list(store) == [6, 7, 8, 9]


# ---------- подписки на новые события и /cancel (ТЗ §6.2) ----------

def callbacks_of(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def test_subscribe_flow_stores_only_the_chat_and_the_filters(dispatcher, wired, session):
    wired.say(dispatcher, "/subscribe")
    assert "Выберите город" in wired.last_text
    cities = callbacks_of(wired.last_markup)
    assert "fs:c:-" in cities and "fs:c:Уфа" in cities
    wired.press(dispatcher, "fs:c:Уфа")
    assert "fs:k:theatre" in callbacks_of(wired.last_markup) and "fs:k:-" in callbacks_of(wired.last_markup)
    wired.press(dispatcher, "fs:k:theatre")
    assert "Подписал: Уфа · Театр" in wired.last_text and "не чаще раза в сутки" in wired.last_text
    (sub,) = subscriptions.list_for(session, CHAT)
    assert (sub.chat_id, sub.city, sub.category) == (CHAT, "Уфа", "theatre")


def test_whole_republic_and_any_category_are_allowed(dispatcher, wired, session):
    wired.say(dispatcher, "/subscribe")
    wired.press(dispatcher, "fs:c:-")
    wired.press(dispatcher, "fs:k:-")
    (sub,) = subscriptions.list_for(session, CHAT)
    assert (sub.city, sub.category) == ("", "")
    wired.press(dispatcher, "fs:c:-")
    wired.press(dispatcher, "fs:k:-")
    assert "уже есть" in wired.last_text and len(subscriptions.list_for(session, CHAT)) == 1


def test_cancel_drops_an_unfinished_subscription(dispatcher, wired, session):
    wired.say(dispatcher, "/cancel")
    assert "Отменять нечего" in wired.last_text
    wired.press(dispatcher, "fs:c:Уфа")
    wired.say(dispatcher, "/cancel")
    assert "Подписка не оформлена" in wired.last_text and wired.last_markup.inline_keyboard
    wired.press(dispatcher, "fs:k:theatre")  # кнопка осталась в чате, но выбор города уже забыт
    assert wired.cb_answer.call_args.kwargs.get("show_alert") is True
    assert subscriptions.list_for(session, CHAT) == []


@pytest.mark.parametrize("data", ["fs:c:Несуществующий", "fs:k:<script>", "fs:k:theatre", "fs:d:abc", "fs:d:"])
def test_forged_or_stale_subscription_buttons_do_nothing(dispatcher, wired, session, data):
    wired.press(dispatcher, data)  # для fs:k:theatre черновика нет: город не выбран
    assert wired.cb_answer.call_args.kwargs.get("show_alert") is True
    assert subscriptions.list_for(session, CHAT) == []


def test_unsubscribe_is_one_command_and_wipes_everything(dispatcher, wired, session):
    subscriptions.add(session, CHAT, "Уфа", "theatre")
    subscriptions.add(session, CHAT, "", "")
    subscriptions.add(session, CHAT + 1, "Уфа", "")  # чужая подписка остаётся
    wired.say(dispatcher, "/unsubscribe")
    assert "Подписки удалены (2)" in wired.last_text
    assert subscriptions.list_for(session, CHAT) == [] and len(subscriptions.list_for(session, CHAT + 1)) == 1
    wired.say(dispatcher, "/unsubscribe")
    assert wired.last_text == "Подписок нет."


def test_list_of_subscriptions_lets_you_delete_one(dispatcher, wired, session):
    subscriptions.add(session, CHAT, "Уфа", "theatre")
    subscriptions.add(session, CHAT, "", "concert")
    wired.say(dispatcher, "/subscriptions")
    assert "Уфа · Театр" in wired.last_text
    first = next(c for c in callbacks_of(wired.last_markup) if c.startswith("fs:d:"))
    wired.press(dispatcher, first)
    assert len(subscriptions.list_for(session, CHAT)) == 1
    wired.press(dispatcher, "fs:da")
    assert subscriptions.list_for(session, CHAT) == []


def test_menu_and_help_lead_to_subscriptions_and_state_the_privacy_terms(dispatcher, wired):
    wired.say(dispatcher, "/start")
    assert "fs:list" in callbacks_of(wired.last_markup) and "/subscribe" in wired.last_text
    wired.say(dispatcher, "/help")
    for needle in ("/subscribe", "/unsubscribe", "/cancel", "номер чата", "12 месяцев"):
        assert needle in wired.last_text, needle


@pytest.fixture
def digest_ready(session, add, monkeypatch):
    monkeypatch.setattr(bot, "SessionLocal", lambda: _Ctx(session))
    send = AsyncMock()
    monkeypatch.setattr(Bot, "send_message", send)
    subscriptions.add(session, CHAT, "Уфа", "")
    sub = subscriptions.list_for(session, CHAT)[0]
    sub.created_at = sub.checked_at = datetime(2026, 11, 9)
    event, _ = add("Новый концерт", datetime(2026, 11, 12, 19, 0), city="Уфа", category="concert", price_min=500)
    event.created_at = datetime(2026, 11, 10, 10, 0)
    session.commit()
    return send


NOON = datetime(2026, 11, 10, 12, 0)


def test_digest_goes_out_once_with_buttons_and_the_unsubscribe_hint(session, digest_ready):
    assert asyncio.run(bot.send_digests(Bot(TOKEN), NOON)) == 1
    chat_id, text = digest_ready.call_args.args[:2]
    assert chat_id == CHAT and "Новый концерт" in text and "/unsubscribe" in text
    assert any(c.startswith("e:") for c in callbacks_of(digest_ready.call_args.kwargs["reply_markup"]))
    assert asyncio.run(bot.send_digests(Bot(TOKEN), NOON + timedelta(hours=1))) == 0  # в тот же день повторно не пишем
    assert digest_ready.await_count == 1


def test_blocked_bot_removes_the_subscription_and_a_flaky_send_keeps_it(session, digest_ready):
    digest_ready.side_effect = TelegramBadRequest(method=None, message="Bad Request: temporary")
    assert asyncio.run(bot.send_digests(Bot(TOKEN), NOON)) == 0
    assert len(subscriptions.list_for(session, CHAT)) == 1  # сбой временный: попробуем в следующий раз
    digest_ready.side_effect = TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")
    assert asyncio.run(bot.send_digests(Bot(TOKEN), NOON)) == 0
    assert subscriptions.list_for(session, CHAT) == []  # заблокировал бота: данные стираем
