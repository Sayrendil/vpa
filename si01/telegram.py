"""Telegram-слой: личка с человеком и закрытый админ-чат. Ничего «умного» здесь не решается."""

import asyncio
import html
import logging
import random
from collections import defaultdict

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as TgMessage
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy import delete, select

from . import flows
from .actions import ADMIN_KINDS
from .brain import ERROR_REPLY, Brain
from .config import Settings
from .db import AdminRequest, Database, Message, TurnLog, User
from .facts import Facts
from .states import Stage
from .stt import SpeechToText

log = logging.getLogger(__name__)

LABELS = {"GOOD", "TOO_ROBOTIC", "TOO_SALESY", "QUESTIONNAIRE", "INVENTED_FACT", "LOST_CONTEXT",
          "WRONG_STATE", "TOO_LONG", "TOO_FAMILIAR", "OTHER"}
VERIFY_KINDS = {"verification_media", "verification_call"}
KIND_TITLES = {"verification_media": "🟡 Проверка: материал", "verification_call": "📞 Просит проверку созвоном",
               **{k: f"🔔 {v}" for k, v in ADMIN_KINDS.items()}}


def who(user: User) -> str:
    name = html.escape(user.first_name or "без имени")
    un = f"@{html.escape(user.username)}" if user.username else "без username"
    return f'<a href="tg://user?id={user.telegram_id}">{name}</a> ({un}, id <code>{user.telegram_id}</code>)'


class TelegramOutbox:
    """Notifier для действий модели + Outbox для flows. Ссылку на оплату отправляем после текста ответа."""

    def __init__(self, bot: Bot, settings: Settings, facts: Facts):
        self.bot, self.s, self.facts = bot, settings, facts
        self.deferred: list[int] = []

    def _verify_kb(self, req_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"vr:a:{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"vr:r:{req_id}"),
        ]])

    def _close_kb(self, req_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✔️ Закрыть", callback_data=f"rq:c:{req_id}"),
        ]])

    async def admin_request(self, user: User, req: AdminRequest, dialog_tail: str,
                            reply_to: int | None = None) -> int:
        parts = [f"<b>{KIND_TITLES.get(req.kind, req.kind)}</b> #{req.id}", f"От: {who(user)}",
                 f"Этап: {user.stage}"]
        if req.summary:
            parts.append(f"\n<b>Суть:</b> {html.escape(req.summary)}")
        if dialog_tail:
            parts.append(f"\n<b>Последние сообщения:</b>\n<blockquote expandable>{html.escape(dialog_tail[-3000:])}</blockquote>")
        parts.append("\n<i>Ответьте реплаем на это сообщение — SI-01 перешлёт текст человеку.</i>")
        kb = self._verify_kb(req.id) if req.kind in VERIFY_KINDS else self._close_kb(req.id)
        msg = await self.bot.send_message(self.s.admin_chat_id, "\n".join(parts), parse_mode="HTML",
                                          reply_markup=kb, reply_to_message_id=reply_to)
        return msg.message_id

    async def send_payment_link(self, user: User) -> None:
        self.deferred.append(user.telegram_id)

    async def flush(self) -> None:
        while self.deferred:
            chat_id = self.deferred.pop(0)
            try:
                await self._send_link(chat_id)
            except Exception:
                log.exception("payment link to %s failed", chat_id)
                await self.admin_text(f"⚠️ Не удалось отправить ссылку на оплату пользователю {chat_id}. "
                                      "Проверьте TRIBUTE_PAYMENT_URL и пришлите ссылку вручную.")

    async def _send_link(self, chat_id: int) -> None:
        if not self.s.tribute_payment_url:
            await self.bot.send_message(chat_id, "Ссылку на оплату пришлём сюда чуть позже.")
            await self.admin_text(f"⚠️ TRIBUTE_PAYMENT_URL не настроен — пользователь {chat_id} одобрен, "
                                  "но ссылку на оплату не получил.")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Оплатить в Tribute", url=self.s.tribute_payment_url)]])
        await self.bot.send_message(chat_id, f"Подписка VPA — {self.facts.price_line}", reply_markup=kb)

    async def send_text(self, chat_id: int, text: str) -> None:
        await self.bot.send_message(chat_id, text)

    async def admin_text(self, text: str) -> None:
        await self.bot.send_message(self.s.admin_chat_id, text)


class UserLocks:
    def __init__(self):
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def __call__(self, uid: int) -> asyncio.Lock:
        return self._locks[uid]


async def get_user(db, tg_user) -> User:
    user = await db.get(User, tg_user.id)
    if user is None:
        user = User(telegram_id=tg_user.id)
        db.add(user)
    user.username, user.first_name = tg_user.username, tg_user.first_name
    return user


def build_router(settings: Settings, database: Database, brain: Brain, facts: Facts,
                 greetings: list[str], stt: SpeechToText | None) -> Router:
    root = Router()
    private = Router()
    private.message.filter(F.chat.type == ChatType.PRIVATE)
    admin = Router()
    admin.message.filter(F.chat.id == settings.admin_chat_id)
    root.include_routers(admin, private)
    locks = UserLocks()

    def is_admin(uid: int | None) -> bool:
        return uid in settings.admin_ids

    # ---------------- личка ----------------

    async def converse(msg: TgMessage, bot: Bot, text: str) -> None:
        async with locks(msg.from_user.id), ChatActionSender.typing(bot=bot, chat_id=msg.chat.id):
            out = TelegramOutbox(bot, settings, facts)
            try:
                async with database.session() as db:
                    user = await get_user(db, msg.from_user)
                    result = await brain.handle(db, user, text, out)
                    sent = await msg.answer(result.text)
                    if result.turn_log:
                        result.turn_log.reply_message_id = sent.message_id
                    await db.commit()
            except Exception:
                log.exception("turn failed for %s", msg.from_user.id)
                await msg.answer(ERROR_REPLY)
                return
            await out.flush()

    @private.message(CommandStart())
    async def on_start(msg: TgMessage):
        text = random.choice(greetings)
        async with locks(msg.from_user.id), database.session() as db:
            user = await get_user(db, msg.from_user)
            await brain.touch_session(db, user)
            await flows.log_assistant(db, user, text)
            await db.commit()
        await msg.answer(text)

    @private.message(Command("reset"))
    async def on_reset(msg: TgMessage):
        """Только для админов при тестах: сбросить себя в NEW и стереть разговор."""
        if not is_admin(msg.from_user.id):
            return await converse(msg, msg.bot, msg.text)
        async with database.session() as db:
            await db.execute(delete(Message).where(Message.telegram_id == msg.from_user.id))
            user = await db.get(User, msg.from_user.id)
            if user:
                await db.delete(user)
            await db.commit()
        await msg.answer("🧹 Сброшено: этап NEW, история пустая.")

    @private.message(Command("label"))
    async def on_label(msg: TgMessage, command: CommandObject):
        """Ответом на сообщение бота: /label TOO_SALESY комментарий — разметка хода для разбора."""
        if not is_admin(msg.from_user.id):
            return await converse(msg, msg.bot, msg.text)
        args = (command.args or "").split(maxsplit=1)
        if not msg.reply_to_message or not args or args[0].upper() not in LABELS:
            return await msg.answer("Ответь на сообщение бота: /label МЕТКА [комментарий]\nМетки: " + ", ".join(sorted(LABELS)))
        async with database.session() as db:
            turn = await db.scalar(select(TurnLog).where(TurnLog.reply_message_id == msg.reply_to_message.message_id,
                                                         TurnLog.telegram_id == msg.from_user.id))
            if not turn:
                return await msg.answer("Не нашёл этот ход в логе.")
            turn.label, turn.label_note = args[0].upper(), (args[1] if len(args) > 1 else None)
            await db.commit()
        await msg.answer(f"🏷 #{turn.id}: {turn.label}")

    @private.message(F.text)
    async def on_text(msg: TgMessage, bot: Bot):
        await converse(msg, bot, msg.text)

    @private.message(F.video_note | F.voice)
    async def on_voice_or_circle(msg: TgMessage, bot: Bot):
        media_kind = "кружок" if msg.video_note else "голосовое"
        async with locks(msg.from_user.id):
            async with database.session() as db:
                user = await get_user(db, msg.from_user)
                await brain.touch_session(db, user)
                # Голосовое — материал проверки, только когда проверку уже начали; кружок — всегда до вступления.
                as_verification = user.stage == Stage.VERIFICATION_PENDING or (
                    msg.video_note and user.stage in (Stage.NEW, Stage.EXPLORING)) or (
                    user.stage == Stage.VERIFICATION_REVIEW)
                if as_verification:
                    if user.stage != Stage.VERIFICATION_REVIEW:
                        # Сначала доставляем админам — этап меняем, только если доставка удалась.
                        out = TelegramOutbox(bot, settings, facts)
                        req = AdminRequest(telegram_id=user.telegram_id, kind="verification_media",
                                           summary=f"Прислал {media_kind}.")
                        db.add(req)
                        await db.flush()
                        try:
                            copied = await bot.copy_message(settings.admin_chat_id, msg.chat.id, msg.message_id)
                            req.admin_message_id = await out.admin_request(user, req, "", reply_to=copied.message_id)
                        except Exception:
                            log.exception("cannot deliver verification to admin chat")
                            await db.rollback()
                            return await msg.answer("Не смог передать на проверку — у меня сбой. Пришли ещё раз чуть позже?")
                    reply = await flows.verification_submitted(db, user, media_kind)
                    await db.commit()
                    return await msg.answer(reply)
                await db.commit()
        text = None
        if msg.voice and stt:
            file = await bot.download(msg.voice)
            heard = await stt.transcribe(file.read())
            text = f"[голосовое] {heard}" if heard else "[прислал голосовое, но распознать не удалось]"
        await converse(msg, bot, text or f"[прислал {media_kind} — содержимое тебе недоступно]")

    @private.message()
    async def on_other(msg: TgMessage, bot: Bot):
        if msg.sticker:
            desc = f"[стикер {msg.sticker.emoji or ''}]"
        elif msg.photo:
            desc = "[прислал фото — ты его не видишь]"
        elif msg.document:
            desc = "[прислал файл — ты его не видишь]"
        else:
            desc = "[прислал вложение, которое ты не видишь]"
        if msg.caption:
            desc += f" подпись: {msg.caption}"
        await converse(msg, bot, desc)

    # ---------------- настройка ----------------

    @root.message(Command("chatid"), F.chat.type != ChatType.PRIVATE)
    async def cmd_chatid(msg: TgMessage):
        """Добавь бота в админ-чат и напиши /chatid — получишь значение для ADMIN_CHAT_ID."""
        await msg.reply(f"chat_id: <code>{msg.chat.id}</code>\nтвой user_id: <code>{msg.from_user.id}</code>",
                        parse_mode="HTML")

    # ---------------- админ-чат ----------------

    @root.callback_query(F.data.startswith("vr:"))
    async def on_verify_decision(cb: CallbackQuery, bot: Bot):
        if not is_admin(cb.from_user.id):
            return await cb.answer("Нет прав", show_alert=True)
        _, action, req_id = cb.data.split(":")
        async with database.session() as db:
            req = await db.get(AdminRequest, int(req_id))
            if not req or req.status != "open":
                return await cb.answer("Уже решено")
            user = await db.get(User, req.telegram_id)
            async with locks(user.telegram_id):
                out = TelegramOutbox(bot, settings, facts)
                note = await flows.decide(db, out, user, approve=(action == "a"), admin_id=cb.from_user.id, req=req)
                await db.commit()
                await out.flush()
        by = f"@{cb.from_user.username}" if cb.from_user.username else cb.from_user.full_name
        await cb.message.edit_text(f"{cb.message.html_text}\n\n<b>{note}</b> — {html.escape(by)}",
                                   parse_mode="HTML", reply_markup=None)
        await cb.answer()

    @root.callback_query(F.data.startswith("rq:c:"))
    async def on_close_request(cb: CallbackQuery):
        if not is_admin(cb.from_user.id):
            return await cb.answer("Нет прав", show_alert=True)
        async with database.session() as db:
            req = await db.get(AdminRequest, int(cb.data.split(":")[2]))
            if req and req.status == "open":
                req.status, req.resolved_by = "closed", cb.from_user.id
                await db.commit()
        await cb.message.edit_text(f"{cb.message.html_text}\n\n<b>✔️ Закрыто</b>", parse_mode="HTML", reply_markup=None)
        await cb.answer()

    @admin.message(Command("user"))
    async def cmd_user(msg: TgMessage, command: CommandObject):
        if not is_admin(msg.from_user.id) or not (command.args or "").strip().isdigit():
            return await msg.answer("/user <telegram_id>")
        async with database.session() as db:
            u = await db.get(User, int(command.args))
            if not u:
                return await msg.answer("Не знаю такого.")
            await msg.answer(f"{who(u)}\nэтап: {u.stage}\nverification: {u.verification_status}\n"
                             f"payment: {u.payment_status}\nmembership: {u.membership_status}\n"
                             f"до: {u.membership_expires_at or '—'}", parse_mode="HTML")

    @admin.message(Command("approve", "reject"))
    async def cmd_decide(msg: TgMessage, command: CommandObject, bot: Bot):
        if not is_admin(msg.from_user.id) or not (command.args or "").strip().isdigit():
            return await msg.answer(f"/{command.command} <telegram_id>")
        async with database.session() as db:
            user = await db.get(User, int(command.args))
            if not user:
                return await msg.answer("Не знаю такого.")
            req = await db.scalar(select(AdminRequest).where(
                AdminRequest.telegram_id == user.telegram_id, AdminRequest.status == "open",
                AdminRequest.kind.in_(VERIFY_KINDS)))
            out = TelegramOutbox(bot, settings, facts)
            note = await flows.decide(db, out, user, approve=command.command == "approve",
                                      admin_id=msg.from_user.id, req=req)
            await db.commit()
            await out.flush()
        await msg.answer(note)

    @admin.message(F.reply_to_message & F.text & ~F.text.startswith("/"))
    async def relay_reply(msg: TgMessage, bot: Bot):
        """Реплай админа на карточку запроса -> сообщение человеку от имени администрации."""
        if not is_admin(msg.from_user.id):
            return
        async with database.session() as db:
            req = await db.scalar(select(AdminRequest).where(
                AdminRequest.admin_message_id == msg.reply_to_message.message_id))
            if not req:
                return
            user = await db.get(User, req.telegram_id)
            text = f"Сообщение от администрации VPA:\n\n{msg.text}"
            await bot.send_message(user.telegram_id, text)
            await flows.log_assistant(db, user, text)
            await db.commit()
        await msg.reply("📨 Отправлено")

    return root
