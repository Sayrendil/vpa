"""Действия администрации, общие для веб-админки и меню в боте: сводка, решение по проверке,
закрытие запроса, сообщение человеку. Кнопки в админ-чате (telegram.py) делают то же самое по-своему —
там решение отражается правкой самой карточки.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from aiogram import Bot
from sqlalchemy import func, select

from . import flows
from .config import Settings
from .db import AdminRequest, Database, TurnLog, User, utcnow
from .facts import Facts
from .states import Stage
from .telegram import VERIFY_KINDS, TelegramOutbox, UserLocks

log = logging.getLogger(__name__)

ADMIN_MESSAGE_PREFIX = "Сообщение от администрации VPA:\n\n"


@dataclass
class Result:
    ok: bool
    text: str


@dataclass
class Overview:
    stages: list[tuple[str, int]]
    total: int
    users_day: int
    open_count: int
    turns_day: int
    fallbacks_day: int
    labels: list[tuple[str, int]]


class AdminOps:
    def __init__(self, settings: Settings, database: Database, bot: Bot, facts: Facts, locks: UserLocks):
        self.s, self.database, self.bot, self.facts, self.locks = settings, database, bot, facts, locks

    async def overview(self) -> Overview:
        day_ago = utcnow() - timedelta(days=1)
        async with self.database.session() as db:
            by_stage = dict((await db.execute(select(User.stage, func.count()).group_by(User.stage))).all())
            count = lambda model, *where: db.scalar(select(func.count()).select_from(model).where(*where))  # noqa: E731
            users_day = await count(User, User.created_at >= day_ago)
            open_count = await count(AdminRequest, AdminRequest.status == "open")
            turns_day = await count(TurnLog, TurnLog.created_at >= day_ago)
            fallbacks_day = await count(TurnLog, TurnLog.created_at >= day_ago, TurnLog.fallback_used.is_not(None))
            labels = dict((await db.execute(select(TurnLog.label, func.count()).where(TurnLog.label.is_not(None))
                                            .group_by(TurnLog.label))).all())
        return Overview(stages=[(s.value, by_stage.get(s.value, 0)) for s in Stage], total=sum(by_stage.values()),
                        users_day=users_day, open_count=open_count, turns_day=turns_day, fallbacks_day=fallbacks_day,
                        labels=sorted(labels.items(), key=lambda kv: -kv[1]))

    async def decide(self, uid: int, approve: bool, admin_id: int, via: str) -> Result:
        """Одобрить/отклонить проверку. via — откуда решение, для отметки в админ-чате."""
        async with self.locks(uid), self.database.session() as db:
            user = await db.get(User, uid)
            if not user:
                return Result(False, "Не знаю такого человека")
            req = await db.scalar(select(AdminRequest).where(
                AdminRequest.telegram_id == uid, AdminRequest.status == "open", AdminRequest.kind.in_(VERIFY_KINDS))
                .order_by(AdminRequest.created_at.desc()))
            out = TelegramOutbox(self.bot, self.s, self.facts)
            stage_before = user.stage
            try:
                note = await flows.decide(db, out, user, approve=approve, admin_id=admin_id, req=req)
            except Exception:
                log.exception("admin: decide failed for %s", uid)
                await db.rollback()
                return Result(False, "Не удалось отправить человеку сообщение — решение не сохранено")
            if user.stage == stage_before:
                return Result(False, note)
            await db.commit()
            await out.flush()
        if req:
            await self.mark_card(req, f"{note} — {via}")
        else:
            await self.admin_chat(f"{note}: {uid} (@{user.username or '—'}) — {via}")
        return Result(True, note)

    async def close_request(self, rid: int, admin_id: int, via: str) -> Result:
        async with self.database.session() as db:
            req = await db.get(AdminRequest, rid)
            if not req or req.status != "open":
                return Result(False, "Запрос уже закрыт")
            req.status, req.resolved_by, req.resolved_at = "closed", admin_id, utcnow()
            await db.commit()
        await self.mark_card(req, f"✔️ Закрыто — {via}")
        return Result(True, f"Запрос #{rid} закрыт")

    async def message_user(self, uid: int, text: str) -> Result:
        text = text.strip()
        if not text:
            return Result(False, "Пустое сообщение")
        full = ADMIN_MESSAGE_PREFIX + text
        async with self.locks(uid), self.database.session() as db:
            user = await db.get(User, uid)
            if not user:
                return Result(False, "Не знаю такого человека")
            try:
                await self.bot.send_message(uid, full)
            except Exception as e:
                log.warning("admin: cannot message %s: %s", uid, e)
                return Result(False, f"Telegram не доставил: {e}")
            await flows.log_assistant(db, user, full)
            await db.commit()
        return Result(True, "📨 Отправлено")

    async def mark_card(self, req: AdminRequest, note: str) -> None:
        """Убрать кнопки с карточки в админ-чате и ответить на неё, чтобы в чате было видно решение."""
        if not req.admin_message_id or not self.s.admin_chat_id:
            return
        try:
            await self.bot.edit_message_reply_markup(chat_id=self.s.admin_chat_id, message_id=req.admin_message_id,
                                                     reply_markup=None)
        except Exception:
            pass
        await self.admin_chat(note, reply_to=req.admin_message_id)

    async def admin_chat(self, text: str, reply_to: int | None = None) -> None:
        if not self.s.admin_chat_id:
            return
        try:
            await self.bot.send_message(self.s.admin_chat_id, text, reply_to_message_id=reply_to)
        except Exception:
            log.exception("admin: cannot post to admin chat")
