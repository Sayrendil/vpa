"""Проводка Telegram-слоя без сети: апдейты подаются в Dispatcher, запросы к Bot API перехватываются."""

from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, CopyMessage, EditMessageText, SendChatAction, SendMessage
from aiogram.types import Chat, MessageId, Update
from aiogram.types import Message as TgMessage

from si01.brain import Brain
from si01.db import AdminRequest, User
from si01.telegram import build_router

from .conftest import FakeClient, block, response

NOW = datetime.now(timezone.utc)


class MockSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self._mid = 500

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            self._mid += 1
            chat_id = getattr(method, "chat_id", 0) or 0
            return TgMessage(message_id=self._mid, date=NOW, chat=Chat(id=chat_id, type="private"), text=method.text)
        if isinstance(method, CopyMessage):
            self._mid += 1
            return MessageId(message_id=self._mid)
        if isinstance(method, (SendChatAction, AnswerCallbackQuery)):
            return True
        raise AssertionError(f"unexpected {type(method).__name__}")

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):
        yield b""

    def sent(self, chat_id=None):
        return [r for r in self.requests if isinstance(r, SendMessage) and (chat_id is None or r.chat_id == chat_id)]


def private_update(uid, **msg):
    return Update(update_id=uid * 1000 + len(msg), message={
        "message_id": 1, "date": NOW, "chat": {"id": 42, "type": "private"},
        "from": {"id": 42, "is_bot": False, "first_name": "Ваня", "username": "vanya"}, **msg})


async def make(settings, facts, database, client):
    session = MockSession()
    bot = Bot("123:abc", session=session)
    dp = Dispatcher()
    dp.include_router(build_router(settings, database, Brain(settings, facts, client), facts, ["Хэлоу броу)"], None))
    return bot, dp, session


async def test_text_goes_through_brain(settings, facts, database):
    client = FakeClient(response(block("text", text="Сейчас 2700 ₽ в месяц.")))
    bot, dp, session = await make(settings, facts, database, client)
    await dp.feed_update(bot, private_update(1, text="сколько стоит?"))
    assert [m.text for m in session.sent(42)] == ["Сейчас 2700 ₽ в месяц."]
    async with database.session() as db:
        assert (await db.get(User, 42)).stage == "EXPLORING"


async def test_start_is_canned(settings, facts, database):
    bot, dp, session = await make(settings, facts, database, FakeClient())
    await dp.feed_update(bot, private_update(2, text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}]))
    assert session.sent(42)[0].text == "Хэлоу броу)"


async def test_circle_to_admins_then_approve_sends_link(settings, facts, database):
    bot, dp, session = await make(settings, facts, database, FakeClient())
    async with database.session() as db:
        db.add(User(telegram_id=42, stage="VERIFICATION_PENDING", verification_status="awaiting_material"))
        await db.commit()
    await dp.feed_update(bot, private_update(3, video_note={"file_id": "f", "file_unique_id": "u", "length": 240, "duration": 5}))
    assert any(isinstance(r, CopyMessage) and r.chat_id == -100 for r in session.requests)
    card = session.sent(-100)[0]
    assert "Проверка" in card.text and card.reply_markup.inline_keyboard[0][0].callback_data.startswith("vr:a:")
    assert "напишем" in session.sent(42)[0].text
    async with database.session() as db:
        assert (await db.get(User, 42)).stage == "VERIFICATION_REVIEW"
        req_id = (await db.scalars(AdminRequest.__table__.select())).first()

    def cb(update_id, from_id):
        return Update(update_id=update_id, callback_query={
            "id": "c1", "chat_instance": "x", "data": f"vr:a:{req_id}",
            "from": {"id": from_id, "is_bot": False, "first_name": "Кто-то"},
            "message": {"message_id": 777, "date": NOW, "chat": {"id": -100, "type": "supergroup"}, "text": "card"}})

    await dp.feed_update(bot, cb(99, 7))  # не админ — ничего не меняется
    async with database.session() as db:
        assert (await db.get(User, 42)).stage == "VERIFICATION_REVIEW"

    await dp.feed_update(bot, cb(100, 1))
    async with database.session() as db:
        u = await db.get(User, 42)
        assert u.stage == "PAYMENT_PENDING" and u.verification_status == "approved"
    link = [m for m in session.sent(42) if m.reply_markup][-1]
    assert link.reply_markup.inline_keyboard[0][0].url == settings.tribute_payment_url
    assert any(isinstance(r, EditMessageText) for r in session.requests)


async def test_voice_without_stt_goes_to_model(settings, facts, database):
    client = FakeClient(response(block("text", text="Голосовые пока не слушаю — напиши текстом?")))
    bot, dp, session = await make(settings, facts, database, client)
    await dp.feed_update(bot, private_update(4, voice={"file_id": "f", "file_unique_id": "u", "duration": 3}))
    assert "голосовое" in client.calls[0]["messages"][0]["content"]
    assert session.sent(42)[0].text.startswith("Голосовые")


async def test_approve_without_payment_url_does_not_crash(settings, facts, database):
    settings.tribute_payment_url = ""
    bot, dp, session = await make(settings, facts, database, FakeClient())
    async with database.session() as db:
        db.add(User(telegram_id=42, stage="VERIFICATION_REVIEW", verification_status="under_review"))
        db.add(AdminRequest(telegram_id=42, kind="verification_media"))
        await db.commit()
    await dp.feed_update(bot, Update(update_id=200, callback_query={
        "id": "c2", "chat_instance": "x", "data": "vr:a:1", "from": {"id": 1, "is_bot": False, "first_name": "A"},
        "message": {"message_id": 777, "date": NOW, "chat": {"id": -100, "type": "supergroup"}, "text": "card"}}))
    assert any("позже" in m.text for m in session.sent(42))
    assert any("TRIBUTE_PAYMENT_URL" in m.text for m in session.sent(-100))
    assert any(isinstance(r, EditMessageText) for r in session.requests)
