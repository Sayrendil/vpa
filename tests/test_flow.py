import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from si01 import flows
from si01.actions import Actions
from si01.brain import ERROR_REPLY, Brain
from si01.db import Message, TurnLog, User
from si01.facts import load_greetings
from si01.states import Source, Stage, TransitionError, can_transition, refresh_membership, transition
from si01.tribute import process_event, verify_signature

from .conftest import FakeClient, FakeOutbox, block, response


# ---------- стейт-машина ----------

def test_model_cannot_verify_or_pay():
    for to in (Stage.PAYMENT_PENDING, Stage.MEMBER, Stage.REJECTED):
        for frm in Stage:
            assert not can_transition(frm, to, Source.MODEL)


def test_only_admin_approves_and_only_tribute_makes_member():
    assert can_transition(Stage.VERIFICATION_REVIEW, Stage.PAYMENT_PENDING, Source.ADMIN)
    assert not can_transition(Stage.VERIFICATION_REVIEW, Stage.PAYMENT_PENDING, Source.MEDIA)
    assert can_transition(Stage.PAYMENT_PENDING, Stage.MEMBER, Source.TRIBUTE)
    assert not can_transition(Stage.PAYMENT_PENDING, Stage.MEMBER, Source.ADMIN)


def test_transition_raises():
    u = User(telegram_id=1, stage="EXPLORING")
    with pytest.raises(TransitionError):
        transition(u, Stage.PAYMENT_PENDING, Source.MODEL)


def test_cancelled_expires():
    u = User(telegram_id=1, stage="MEMBER_CANCELLED",
             membership_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    refresh_membership(u)
    assert u.stage == Stage.EXPIRED


def test_facts_render(facts):
    assert "2700" in facts.text and "НЕ ЗАФИКСИРОВАНО" in facts.text
    assert "К-Книги" in facts.text and "крипт" not in facts.text.lower()


def test_greetings(settings):
    assert len(load_greetings(settings.greetings_path)) >= 3


# ---------- действия ----------

async def test_begin_verification_then_no_payment(db, user):
    user.stage = "EXPLORING"
    out = FakeOutbox()
    a = Actions(db, user, out, payment_url_configured=True)
    assert (await a.run("begin_verification", {})).ok
    assert user.stage == Stage.VERIFICATION_PENDING
    res = await a.run("resend_payment_link", {})
    assert not res.ok and out.links == []


async def test_call_verification_goes_to_admins(db, user):
    user.stage = "VERIFICATION_PENDING"
    out = FakeOutbox()
    res = await Actions(db, user, out, True).run("request_call_verification", {"note": "не люблю кружки"})
    assert res.ok and user.stage == Stage.VERIFICATION_REVIEW
    assert out.admin_requests[0][1] == "verification_call"


async def test_admin_failure_is_not_success(db, user):
    user.stage = "EXPLORING"
    res = await Actions(db, user, FakeOutbox(fail=True), True).run("notify_admins", {"kind": "human", "summary": "x"})
    assert not res.ok


async def test_notify_admins_dedup(db, user):
    out = FakeOutbox()
    a = Actions(db, user, out, True)
    await a.run("notify_admins", {"kind": "human", "summary": "позови Артура"})
    await a.run("notify_admins", {"kind": "human", "summary": "ну позови"})
    assert len(out.admin_requests) == 1


async def test_rejected_cannot_restart(db, user):
    user.stage = "REJECTED"
    assert not (await Actions(db, user, FakeOutbox(), True).run("begin_verification", {})).ok
    assert user.stage == Stage.REJECTED


# ---------- путь вступления целиком ----------

async def test_full_entry_path(db, user, settings, database):
    out = FakeOutbox()
    user.stage = "EXPLORING"
    await Actions(db, user, out, True).run("begin_verification", {})
    assert await flows.verification_submitted(db, user, "кружок")
    assert user.stage == Stage.VERIFICATION_REVIEW
    note = await flows.decide(db, out, user, approve=True, admin_id=1)
    assert "Одобрено" in note and user.stage == Stage.PAYMENT_PENDING and out.links == [42]
    await db.commit()

    body = json.dumps({"name": "new_subscription", "created_at": "2026-10-07T10:00:00Z", "sent_at": "2026-10-07T10:00:01Z",
                       "payload": {"subscription_id": 1, "telegram_user_id": 42, "channel_id": 5,
                                   "expires_at": "2026-11-07T10:00:00Z"}}).encode()
    assert await process_event(database, out, settings, body) == "ok"
    assert await process_event(database, out, settings, body) == "duplicate"
    async with database.session() as s2:
        u = await s2.get(User, 42)
        assert u.stage == Stage.MEMBER and u.payment_status == "paid"
    assert out.admin_texts == []  # проверка была одобрена — алерта нет

    cancel = json.dumps({"name": "cancelled_subscription", "payload": {"subscription_id": 1, "telegram_user_id": 42,
                         "expires_at": "2026-11-07T10:00:00Z", "cancel_reason": ""}}).encode()
    await process_event(database, out, settings, cancel)
    async with database.session() as s3:
        assert (await s3.get(User, 42)).stage == Stage.MEMBER_CANCELLED


async def test_paid_without_verification_alerts_admins(database, settings):
    out = FakeOutbox()
    body = json.dumps({"name": "new_subscription", "payload": {"subscription_id": 1, "telegram_user_id": 77,
                       "expires_at": "2026-11-07T10:00:00Z"}}).encode()
    await process_event(database, out, settings, body)
    assert out.admin_texts and "не одобрена" in out.admin_texts[0]


def test_signature():
    body = b'{"name":"x"}'
    sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, "secret")
    assert not verify_signature(body, sig, "other")
    assert not verify_signature(body, None, "secret")


# ---------- оркестратор ----------

async def test_brain_tool_loop_and_context(db, user, settings, facts):
    user.stage = "EXPLORING"
    client = FakeClient(
        response(block("tool_use", id="t1", name="begin_verification", input={}), stop="tool_use"),
        response(block("text", text="Погнали) Пришли кружок или голосовое сюда.")),
    )
    brain = Brain(settings, facts, client)
    res = await brain.handle(db, user, "всё, хочу зайти", FakeOutbox())
    assert res.text.startswith("Погнали") and user.stage == Stage.VERIFICATION_PENDING
    first = client.calls[0]
    assert first["messages"][-1]["role"] == "system" and "EXPLORING" in first["messages"][-1]["content"]
    assert "2700" in first["system"][1]["text"]
    second = client.calls[1]["messages"]
    assert second[-1]["content"][0]["type"] == "tool_result" and not second[-1]["content"][0]["is_error"]
    turn = res.turn_log
    assert turn.stage_before == "EXPLORING" and turn.stage_after == "VERIFICATION_PENDING"
    assert turn.prompt_version == "0.1.0" and turn.facts_version == facts.version


async def test_brain_history_and_session_reset(db, user, settings, facts):
    client = FakeClient(response(block("text", text="Хэлоу)")), response(block("text", text="2700 ₽ в месяц")),
                        response(block("text", text="Привет снова")))
    brain = Brain(settings, facts, client)
    await brain.handle(db, user, "привет", FakeOutbox())
    await brain.handle(db, user, "сколько стоит?", FakeOutbox())
    msgs = client.calls[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "system"]
    # 49 часов тишины -> новая сессия, но этап сохраняется
    user.stage = "VERIFICATION_REVIEW"
    user.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=49)
    await brain.handle(db, user, "ну что там?", FakeOutbox())
    msgs = client.calls[2]["messages"]
    assert [m["role"] for m in msgs] == ["user", "system"] and user.session_no == 2
    assert user.stage == Stage.VERIFICATION_REVIEW


async def test_brain_api_error_not_saved_as_answer(db, user, settings, facts):
    import anthropic, httpx2
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    class Boom(FakeClient):
        async def _create(self, **kw):
            raise anthropic.APIConnectionError(request=req)

    brain = Brain(settings, facts, Boom())
    res = await brain.handle(db, user, "алло", FakeOutbox())
    assert res.text == ERROR_REPLY
    await db.flush()
    rows = (await db.scalars(Message.__table__.select())).all()
    assert [r for r in rows]  # вопрос человека сохранён
    roles = [r.role for r in (await db.execute(Message.__table__.select())).all()]
    assert roles == ["user"]
