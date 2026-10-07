"""Этапы человека и правила переходов.

Критичные переходы делает код/админ/Tribute — модель может только попросить
«начать проверку» через инструмент, и то лишь на ранних этапах.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from .db import User, aware


class Stage(StrEnum):
    NEW = "NEW"                                   # ещё ничего не писал
    EXPLORING = "EXPLORING"                       # общается, смотрит
    VERIFICATION_PENDING = "VERIFICATION_PENDING" # хочет вступить, ждём кружок/голосовое
    VERIFICATION_REVIEW = "VERIFICATION_REVIEW"   # материал/запрос созвона у админов
    REJECTED = "REJECTED"                         # админ отклонил
    PAYMENT_PENDING = "PAYMENT_PENDING"           # одобрен, ссылка на оплату выдана
    MEMBER = "MEMBER"                             # Tribute: подписка активна
    MEMBER_CANCELLED = "MEMBER_CANCELLED"         # продление отменено, доступ до конца периода
    EXPIRED = "EXPIRED"                           # оплаченный период закончился после отмены


class Source(StrEnum):
    SYSTEM = "system"   # код: первое сообщение, истечение срока
    MODEL = "model"     # инструмент, вызванный моделью
    MEDIA = "media"     # человек прислал кружок/голосовое на проверку
    ADMIN = "admin"     # кнопка/команда в админ-чате
    TRIBUTE = "tribute" # вебхук оплаты


S = Stage
_PRE_VERIFY = {S.NEW, S.EXPLORING, S.VERIFICATION_PENDING}

# (откуда, куда) -> кто имеет право сделать этот переход
TRANSITIONS: dict[tuple[Stage, Stage], set[Source]] = {
    (S.NEW, S.EXPLORING): {Source.SYSTEM},
    **{(s, S.VERIFICATION_PENDING): {Source.MODEL} for s in (S.NEW, S.EXPLORING)},
    **{(s, S.VERIFICATION_REVIEW): {Source.MEDIA, Source.MODEL} for s in _PRE_VERIFY},
    (S.VERIFICATION_REVIEW, S.PAYMENT_PENDING): {Source.ADMIN},
    (S.VERIFICATION_REVIEW, S.REJECTED): {Source.ADMIN},
    (S.REJECTED, S.PAYMENT_PENDING): {Source.ADMIN},
    (S.MEMBER, S.MEMBER_CANCELLED): {Source.TRIBUTE},
    (S.MEMBER_CANCELLED, S.MEMBER): {Source.TRIBUTE},
    (S.MEMBER_CANCELLED, S.EXPIRED): {Source.SYSTEM},
    (S.EXPIRED, S.MEMBER): {Source.TRIBUTE},
}
# Оплата в Tribute — факт, а не просьба: принимаем из любого этапа (админам уйдёт алерт,
# если человек оплатил, не пройдя проверку).
for _s in Stage:
    if _s != S.MEMBER:
        TRANSITIONS.setdefault((_s, S.MEMBER), set()).add(Source.TRIBUTE)
# Админ может вручную одобрить/отклонить человека на любом доверифицированном этапе.
for _s in _PRE_VERIFY:
    TRANSITIONS.setdefault((_s, S.PAYMENT_PENDING), set()).add(Source.ADMIN)
    TRANSITIONS.setdefault((_s, S.REJECTED), set()).add(Source.ADMIN)


class TransitionError(Exception):
    pass


def can_transition(frm: str, to: str, source: Source) -> bool:
    return source in TRANSITIONS.get((Stage(frm), Stage(to)), set())


def transition(user: User, to: Stage, source: Source) -> None:
    if user.stage == to:
        return
    if not can_transition(user.stage, to, source):
        raise TransitionError(f"{user.stage} -> {to} запрещён для {source}")
    user.stage = to


def refresh_membership(user: User, now: datetime | None = None) -> None:
    """Отменённая подписка после конца оплаченного периода -> EXPIRED."""
    now = now or datetime.now(timezone.utc)
    exp = aware(user.membership_expires_at)
    if user.stage == S.MEMBER_CANCELLED and exp and exp <= now:
        transition(user, S.EXPIRED, Source.SYSTEM)
        user.membership_status = "expired"



@dataclass
class StageInfo:
    meaning: str
    next_step: str


STAGE_INFO: dict[Stage, StageInfo] = {
    S.NEW: StageInfo("Человек только что пришёл.", "Просто разговор."),
    S.EXPLORING: StageInfo(
        "Общается, смотрит, задаёт вопросы. Проверку не начинал.",
        "Если явно хочет вступить — вызови begin_verification и объясни проверку. "
        "Ссылку на оплату на этом этапе дать нельзя.",
    ),
    S.VERIFICATION_PENDING: StageInfo(
        "Хочет вступить, ждём от него кружок или голосовое прямо в этот чат.",
        "Напомни, что нужно прислать кружок или голосовое. Если не может/не хочет — можно "
        "запросить созвон (request_call_verification). Если отказывается от всех трёх — "
        "вступить нельзя. Ссылки на оплату нет, пока админ не одобрит.",
    ),
    S.VERIFICATION_REVIEW: StageInfo(
        "Материал для проверки (или запрос созвона) уже у администрации, ждём решения.",
        "Говори: посмотрим и напишем сюда, как только проверим. Срок не обещай. Решение принимают люди.",
    ),
    S.REJECTED: StageInfo(
        "Администрация отклонила вступление.",
        "Не спорь и не обещай пересмотр. Если человек не согласен — можно передать "
        "администрации (notify_admins, kind=appeal).",
    ),
    S.PAYMENT_PENDING: StageInfo(
        "Проверка одобрена, ссылка на оплату Tribute уже выдана.",
        "Если человек потерял ссылку — resend_payment_link. Оплату считаем подтверждённой "
        "только по статусу от Tribute.",
    ),
    S.MEMBER: StageInfo(
        "Участник VPA, подписка в Tribute активна.",
        "Не продавай VPA повторно. Помогай сориентироваться внутри.",
    ),
    S.MEMBER_CANCELLED: StageInfo(
        "Участник отменил продление в Tribute; доступ сохраняется до конца оплаченного периода.",
        "Не уговаривай остаться.",
    ),
    S.EXPIRED: StageInfo(
        "Был участником, оплаченный период закончился, подписка не продлена.",
        "Повторное вступление: правило не зафиксировано — если хочет вернуться, передай "
        "администрации (notify_admins).",
    ),
}
