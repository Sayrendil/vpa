"""Админ-меню в личке с ботом: апдейты подаются в Dispatcher, запросы к Bot API перехватываются."""

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from si01.admin_bot import build_admin_menu
from si01.admin_ops import AdminOps
from si01.brain import Brain
from si01.db import AdminRequest, Message, TurnLog, User
from si01.telegram import UserLocks, build_router

from .conftest import FakeClient, block, response
from .test_admin_web import PanelSession
from .test_telegram import NOW

ADMIN = 1


async def make(settings, facts, database, client=None):
    session = PanelSession()
    bot = Bot("123:abc", session=session)
    locks = UserLocks()
    dp = Dispatcher()
    dp.include_router(build_admin_menu(AdminOps(settings, database, bot, facts, locks)))
    dp.include_router(build_router(settings, database, Brain(settings, facts, client or FakeClient()), facts,
                                   ["Хэлоу"], None, locks))
    return bot, dp, session


_uid = iter(range(1, 10**6))


def text(from_id, t):
    entities = [{"type": "bot_command", "offset": 0, "length": len(t.split()[0])}] if t.startswith("/") else None
    return Update(update_id=next(_uid), message={
        "message_id": 10, "date": NOW, "chat": {"id": from_id, "type": "private"}, "text": t,
        "from": {"id": from_id, "is_bot": False, "first_name": "Админ", "username": "boss"},
        **({"entities": entities} if entities else {})})


def press(data, from_id=ADMIN):
    return Update(update_id=next(_uid), callback_query={
        "id": "c", "chat_instance": "x", "data": data,
        "from": {"id": from_id, "is_bot": False, "first_name": "Админ", "username": "boss"},
        "message": {"message_id": 900, "date": NOW, "chat": {"id": from_id, "type": "private"}, "text": "menu"}})


def buttons(m):
    return [b.callback_data for row in (m.reply_markup.inline_keyboard if m.reply_markup else []) for b in row]


async def seed(database, **user):
    async with database.session() as db:
        db.add(User(telegram_id=42, first_name="Ваня", username="vanya", **user))
        await db.commit()


async def test_admin_menu_only_for_admins(settings, facts, database):
    client = FakeClient(response(block("text", text="Не понял)")))
    bot, dp, session = await make(settings, facts, database, client)
    await dp.feed_update(bot, text(42, "/admin"))
    assert session.sent(42)[0].text == "Не понял)"  # обычному человеку — просто разговор
    await dp.feed_update(bot, text(ADMIN, "/admin"))
    menu = session.sent(ADMIN)[0]
    assert "Админ-меню" in menu.text and "am:rq:0" in buttons(menu)


async def test_approve_open_request_from_menu(settings, facts, database):
    await seed(database, stage="VERIFICATION_REVIEW", verification_status="under_review")
    async with database.session() as db:
        db.add(AdminRequest(telegram_id=42, kind="verification_media", admin_message_id=777))
        await db.commit()
    bot, dp, session = await make(settings, facts, database)
    await dp.feed_update(bot, press("am:rq:0"))
    card = session.requests[-2]  # EditMessageText, затем AnswerCallbackQuery
    assert "Проверка" in card.text and "am:ok:42:r0" in buttons(card)
    await dp.feed_update(bot, press("am:ok:42:r0"))
    async with database.session() as db:
        u = await db.get(User, 42)
        assert u.stage == "PAYMENT_PENDING" and (await db.get(AdminRequest, 1)).resolved_by == ADMIN
    assert [m for m in session.sent(42) if m.reply_markup][-1].reply_markup.inline_keyboard[0][0].url
    assert any("меню бота, @boss" in m.text for m in session.sent(-100))


async def test_write_to_user_does_not_reach_model(settings, facts, database):
    await seed(database)
    bot, dp, session = await make(settings, facts, database)  # пустой FakeClient: вызов модели упал бы
    await dp.feed_update(bot, press("am:wr:42"))
    await dp.feed_update(bot, text(ADMIN, "Привет, это Артур"))
    assert session.sent(42)[-1].text == "Сообщение от администрации VPA:\n\nПривет, это Артур"
    assert session.sent(ADMIN)[-1].text == "📨 Отправлено"
    async with database.session() as db:
        assert await db.scalar(Message.__table__.select().where(Message.telegram_id == 42))


async def test_search_and_cancel(settings, facts, database):
    await seed(database, stage="EXPLORING")
    bot, dp, session = await make(settings, facts, database)
    await dp.feed_update(bot, press("am:find"))
    await dp.feed_update(bot, text(ADMIN, "@vanya"))
    profile = session.sent(ADMIN)[-1]
    assert "EXPLORING" in profile.text and "am:ok:42:p" in buttons(profile)
    await dp.feed_update(bot, press("am:find"))
    await dp.feed_update(bot, text(ADMIN, "/cancel"))
    assert "Админ-меню" in session.sent(ADMIN)[-1].text


async def test_label_turns(settings, facts, database):
    await seed(database)
    async with database.session() as db:
        for t in ("раз", "два"):
            db.add(TurnLog(telegram_id=42, session_no=1, stage_before="EXPLORING", stage_after="EXPLORING",
                           user_text=t, reply_text="ок", model="m", prompt_version="1", facts_version="1"))
        await db.commit()
    bot, dp, session = await make(settings, facts, database)
    await dp.feed_update(bot, press("am:lb"))
    assert "Ход #2" in session.requests[-2].text
    await dp.feed_update(bot, press("am:lt:2:TOO_SALESY"))
    assert "Ход #1" in session.requests[-2].text
    async with database.session() as db:
        assert (await db.get(TurnLog, 2)).label == "TOO_SALESY"
