"""Детерминированные шаги вступления, которые делают люди и платёжка, а не модель.

Тексты здесь короткие и заранее заданные: решение принял человек/Tribute, модель его не пересказывает.
Всё, что отправлено человеку, пишется в историю сессии — чтобы SI-01 в разговоре знал, что произошло.
"""

import logging
import random
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from .db import AdminRequest, Message, User, aware, utcnow
from .states import Source, Stage, TransitionError, transition

log = logging.getLogger(__name__)

VERIFICATION_RECEIVED = [
    "Получил) Посмотрим и напишем тебе сюда, как только проверим.",
    "Есть, принял. Глянем и напишем сюда, как проверим.",
]
VERIFICATION_ALREADY = "Уже на проверке) Как только посмотрим — напишу сюда."
APPROVED = [
    "Всё, проверку прошёл) Ниже кнопка на оплату — после неё Tribute сам откроет доступ к VPA.",
    "Готово, проверка пройдена) Держи оплату — после неё Tribute даст доступ.",
]
REJECTED = (
    "Посмотрели — к сожалению, вступление сейчас не одобрили. "
    "Если считаешь, что это ошибка, напиши мне — передам администрации."
)
WELCOME = "Оплата прошла, добро пожаловать в VPA) Доступ к комнатам выдаёт Tribute. Если потеряешься внутри — пиши мне."


class Outbox(Protocol):
    async def send_text(self, chat_id: int, text: str) -> None: ...

    async def send_payment_link(self, user: User) -> None: ...

    async def admin_text(self, text: str) -> None: ...


async def log_assistant(db: AsyncSession, user: User, text: str, user_side: str | None = None) -> None:
    if user_side:
        db.add(Message(telegram_id=user.telegram_id, session_no=user.session_no, role="user", content=user_side))
    db.add(Message(telegram_id=user.telegram_id, session_no=user.session_no, role="assistant", content=text))


async def verification_submitted(db: AsyncSession, user: User, media_kind: str) -> str | None:
    """Человек прислал кружок/голосовое на проверку. Возвращает текст ответа или None, если это не проверка."""
    st = Stage(user.stage)
    if st == Stage.VERIFICATION_REVIEW:
        return VERIFICATION_ALREADY
    if st not in (Stage.NEW, Stage.EXPLORING, Stage.VERIFICATION_PENDING):
        return None
    transition(user, Stage.VERIFICATION_REVIEW, Source.MEDIA)
    user.verification_status = "under_review"
    text = random.choice(VERIFICATION_RECEIVED)
    await log_assistant(db, user, text, user_side=f"[прислал {media_kind} для проверки]")
    return text


async def decide(db: AsyncSession, outbox: Outbox, user: User, approve: bool, admin_id: int,
                 req: AdminRequest | None = None) -> str:
    """Решение админа по проверке. Возвращает строку для админ-чата."""
    target = Stage.PAYMENT_PENDING if approve else Stage.REJECTED
    try:
        transition(user, target, Source.ADMIN)
    except TransitionError:
        return f"Нельзя: человек сейчас на этапе {user.stage}."
    if req:
        req.status = "approved" if approve else "rejected"
        req.resolved_by, req.resolved_at = admin_id, utcnow()
    if approve:
        user.verification_status = "approved"
        user.payment_status = "link_sent"
        text = random.choice(APPROVED)
        await outbox.send_text(user.telegram_id, text)
        await outbox.send_payment_link(user)
    else:
        user.verification_status = "rejected"
        text = REJECTED
        await outbox.send_text(user.telegram_id, text)
    await log_assistant(db, user, text)
    return "✅ Одобрено, ссылка на оплату отправлена" if approve else "❌ Отклонено, человеку сообщили"


async def subscription_event(db: AsyncSession, outbox: Outbox, user: User, name: str,
                             expires_at: datetime | None) -> None:
    """Статус подписки по вебхуку Tribute — единственный источник правды об оплате."""
    if name in ("new_subscription", "renewed_subscription"):
        was = Stage(user.stage)
        if was not in (Stage.MEMBER,) and user.verification_status != "approved":
            await outbox.admin_text(
                f"⚠️ Tribute: оплата от {user.telegram_id} (@{user.username or '—'}), "
                f"но проверка не одобрена (этап был {was}). Проверьте вручную."
            )
        transition(user, Stage.MEMBER, Source.TRIBUTE)
        user.payment_status = "paid"
        user.membership_status = "active"
        if expires_at and (not user.membership_expires_at or expires_at > aware(user.membership_expires_at)):
            user.membership_expires_at = expires_at
        if name == "new_subscription" and was not in (Stage.MEMBER, Stage.MEMBER_CANCELLED):
            try:
                await outbox.send_text(user.telegram_id, WELCOME)
                await log_assistant(db, user, WELCOME)
            except Exception:
                log.info("cannot message %s (did not start the bot?)", user.telegram_id)
    elif name == "cancelled_subscription":
        if user.stage == Stage.MEMBER:
            transition(user, Stage.MEMBER_CANCELLED, Source.TRIBUTE)
        user.membership_status = "cancelled"
        if expires_at:
            user.membership_expires_at = expires_at

