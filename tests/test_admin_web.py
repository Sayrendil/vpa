"""Веб-админка без сети: aiohttp TestClient + Bot с подменённой сессией."""

import re

import pytest
from aiogram import Bot
from aiogram.methods import EditMessageReplyMarkup
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from si01.admin_ops import AdminOps
from si01.admin_web import setup_admin
from si01.db import AdminRequest, Message, TurnLog, User
from si01.telegram import UserLocks

from .test_telegram import MockSession


class PanelSession(MockSession):
    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, EditMessageReplyMarkup):
            self.requests.append(method)
            return True
        return await super().make_request(bot, method, timeout)


@pytest.fixture
async def panel(settings, facts, database):
    settings.admin_panel_password = "pw"
    session = PanelSession()
    app = web.Application()
    setup_admin(app, AdminOps(settings, database, Bot("123:abc", session=session), facts, UserLocks()), info={"Модель": "x"})
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client, session
    await client.close()


async def login(client) -> str:
    r = await client.post("/admin/login", data={"password": "pw"})
    assert r.status == 200 and "Обзор" in await r.text()
    return re.search(r'name="csrf" value="([^"]+)"', await r.text()).group(1)


async def test_requires_login(panel):
    client, _ = panel
    r = await client.get("/admin/users", allow_redirects=False)
    assert r.status == 302 and r.headers["Location"] == "/admin/login"
    r = await client.post("/admin/login", data={"password": "nope"})
    assert "Неверный пароль" in await r.text()


async def test_post_without_csrf_is_rejected(panel, database):
    client, _ = panel
    await login(client)
    r = await client.post("/admin/users/42/message", data={"text": "hi"})
    assert r.status == 403


async def test_approve_from_panel_sends_link_and_updates_card(panel, database, settings):
    client, session = panel
    async with database.session() as db:
        db.add(User(telegram_id=42, first_name="Ваня", stage="VERIFICATION_REVIEW", verification_status="under_review"))
        db.add(AdminRequest(telegram_id=42, kind="verification_media", admin_message_id=777))
        await db.commit()
    csrf = await login(client)
    assert "Ваня" in await (await client.get("/admin")).text()
    r = await client.post("/admin/users/42/decide", data={"csrf": csrf, "decision": "approve"})
    assert r.status == 200 and "Одобрено" in await r.text()
    async with database.session() as db:
        u = await db.get(User, 42)
        assert u.stage == "PAYMENT_PENDING" and u.verification_status == "approved"
        assert (await db.get(AdminRequest, 1)).status == "approved"
    link = [m for m in session.sent(42) if m.reply_markup][-1]
    assert link.reply_markup.inline_keyboard[0][0].url == settings.tribute_payment_url
    assert any(isinstance(m, EditMessageReplyMarkup) and m.message_id == 777 for m in session.requests)
    assert any("админ-панель" in m.text for m in session.sent(-100))


async def test_decide_on_wrong_stage_changes_nothing(panel, database):
    client, session = panel
    async with database.session() as db:
        db.add(User(telegram_id=42, stage="MEMBER"))
        await db.commit()
    csrf = await login(client)
    r = await client.post("/admin/users/42/decide", data={"csrf": csrf, "decision": "approve"})
    assert "Нельзя" in await r.text()
    assert not session.sent(42)


async def test_message_and_label(panel, database):
    client, session = panel
    async with database.session() as db:
        db.add(User(telegram_id=42, first_name="Ваня"))
        db.add(TurnLog(telegram_id=42, session_no=1, stage_before="EXPLORING", stage_after="EXPLORING",
                       user_text="привет", reply_text="хэй", model="m", prompt_version="1", facts_version="1"))
        await db.commit()
    csrf = await login(client)
    await client.post("/admin/users/42/message", data={"csrf": csrf, "text": "Привет от нас"})
    assert session.sent(42)[0].text == "Сообщение от администрации VPA:\n\nПривет от нас"
    await client.post("/admin/turns/1/label", data={"csrf": csrf, "label": "TOO_SALESY", "note": "давит"})
    async with database.session() as db:
        t = await db.get(TurnLog, 1)
        assert (t.label, t.label_note) == ("TOO_SALESY", "давит")
        assert await db.scalar(Message.__table__.select().where(Message.telegram_id == 42))
    for page in ("/admin/users", "/admin/users/42", "/admin/requests?status=all", "/admin/turns", "/admin/tribute"):
        assert (await client.get(page)).status == 200, page
