"""Action Layer: единственное, что модель может «сделать». Каждое действие проверяет этап на бэкенде.

Модель получает от инструмента только текст результата. Говорить «сделано» она может,
только если ok=True.
"""

from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import AdminRequest, User
from .states import Source, Stage, transition

ADMIN_KINDS = {
    "human": "Просит живого человека",
    "appeal": "Апелляция / не согласен с решением",
    "payment_reschedule": "Просит перенести дату оплаты",
    "data_deletion": "Просит удалить его данные",
    "rejoin": "Хочет вернуться в VPA",
    "other": "Другое",
}


class Notifier(Protocol):
    """То, что действия делают во внешнем мире. В проде — Telegram, в тестах/evals — запись в список."""

    async def admin_request(self, user: User, req: AdminRequest, dialog_tail: str) -> int | None: ...

    async def send_payment_link(self, user: User) -> None: ...


TOOLS = [
    {
        "name": "begin_verification",
        "description": (
            "Отметить, что человек хочет вступить в VPA и начинает проверку «реальный человек». "
            "Вызывай, только когда человек явно говорит, что хочет вступить/зайти/оплатить, а не "
            "когда просто спрашивает, как устроено вступление. После успеха объясни: нужно прислать "
            "кружок или голосовое прямо в этот чат."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "request_call_verification",
        "description": (
            "Передать администрации просьбу пройти проверку созвоном — запасной вариант, если "
            "человек не может или не хочет присылать кружок/голосовое. Контакты человеку не выдаются, "
            "администрация свяжется сама."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "Коротко: почему созвон и удобное время, если человек сказал.",
                }
            },
            "required": ["note"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "resend_payment_link",
        "description": (
            "Повторно прислать человеку кнопку со ссылкой на оплату Tribute. Работает только если "
            "проверка уже одобрена администрацией. Ссылку в текст ответа не пиши — её пришлёт система."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "notify_admins",
        "description": (
            "Передать запрос в закрытый админ-чат VPA, где отвечают живые люди. Для: просьбы позвать "
            "человека, апелляции, переноса даты оплаты, удаления данных, возвращения в VPA и всего, "
            "что решают люди, а не ты. Исход не обещай: решают люди."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(ADMIN_KINDS)},
                "summary": {
                    "type": "string",
                    "description": (
                        "2–4 предложения для администратора: кто человек (как он сам представился, "
                        "если представился), что хочет и важный контекст разговора — чтобы ему не "
                        "пришлось всё повторять."
                    ),
                },
            },
            "required": ["kind", "summary"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


@dataclass
class ActionResult:
    ok: bool
    message: str


@dataclass
class ActionLog:
    name: str
    input: dict
    ok: bool
    message: str


@dataclass
class Actions:
    db: AsyncSession
    user: User
    notifier: Notifier
    payment_url_configured: bool
    dialog_tail: str = ""
    log: list[ActionLog] = field(default_factory=list)

    async def run(self, name: str, args: dict) -> ActionResult:
        handler = getattr(self, f"_do_{name}", None)
        if handler is None or name not in {t["name"] for t in TOOLS}:
            res = ActionResult(False, f"Неизвестное действие {name}.")
        else:
            try:
                res = await handler(**args)
            except TypeError:
                res = ActionResult(False, "Некорректные параметры действия.")
        self.log.append(ActionLog(name, args, res.ok, res.message))
        return res

    async def _do_begin_verification(self) -> ActionResult:
        st = Stage(self.user.stage)
        if st in (Stage.NEW, Stage.EXPLORING):
            transition(self.user, Stage.VERIFICATION_PENDING, Source.MODEL)
            self.user.verification_status = "awaiting_material"
            return ActionResult(True, "Готово: ждём от человека кружок или голосовое в этот чат.")
        if st == Stage.VERIFICATION_PENDING:
            return ActionResult(True, "Проверка уже начата: ждём кружок или голосовое.")
        if st == Stage.VERIFICATION_REVIEW:
            return ActionResult(False, "Материал уже у администрации на проверке, ждём решения.")
        if st == Stage.REJECTED:
            return ActionResult(False, "Вступление отклонено администрацией. Повторно начать проверку нельзя; "
                                       "можно передать несогласие администрации (notify_admins, appeal).")
        if st == Stage.PAYMENT_PENDING:
            return ActionResult(False, "Проверка уже одобрена, следующий шаг — оплата по ссылке.")
        return ActionResult(False, "Человек уже был участником; проверку заново не начинаем. "
                                   "Если хочет вернуться — notify_admins (rejoin).")

    async def _do_request_call_verification(self, note: str) -> ActionResult:
        st = Stage(self.user.stage)
        if st not in (Stage.NEW, Stage.EXPLORING, Stage.VERIFICATION_PENDING):
            return (await self._do_begin_verification()) if st != Stage.VERIFICATION_REVIEW else \
                ActionResult(False, "Запрос на проверку уже у администрации.")
        req = AdminRequest(telegram_id=self.user.telegram_id, kind="verification_call",
                           summary=note.strip()[:1000])
        self.db.add(req)
        await self.db.flush()
        try:
            req.admin_message_id = await self.notifier.admin_request(self.user, req, self.dialog_tail)
        except Exception:
            await self.db.delete(req)
            return ActionResult(False, "Не получилось отправить запрос администрации — технический сбой. "
                                       "Скажи человеку попробовать чуть позже.")
        transition(self.user, Stage.VERIFICATION_REVIEW, Source.MODEL)
        self.user.verification_status = "call_requested"
        return ActionResult(True, "Запрос на созвон передан администрации. Они свяжутся сами; срок не обещаем.")

    async def _do_resend_payment_link(self) -> ActionResult:
        if self.user.stage != Stage.PAYMENT_PENDING:
            return ActionResult(False, f"Ссылку дать нельзя: текущий этап {self.user.stage}, "
                                       "оплата доступна только после одобрения проверки.")
        if not self.payment_url_configured:
            return ActionResult(False, "Ссылка на оплату не настроена в системе. Передай вопрос администрации.")
        try:
            await self.notifier.send_payment_link(self.user)
        except Exception:
            return ActionResult(False, "Не получилось отправить ссылку — технический сбой.")
        return ActionResult(True, "Кнопка с оплатой отправлена отдельным сообщением сразу после твоего ответа.")

    async def _do_notify_admins(self, kind: str, summary: str) -> ActionResult:
        if kind not in ADMIN_KINDS:
            kind = "other"
        open_same = await self.db.scalar(
            select(AdminRequest).where(
                AdminRequest.telegram_id == self.user.telegram_id,
                AdminRequest.kind == kind,
                AdminRequest.status == "open",
            )
        )
        if open_same:
            return ActionResult(True, "Такой запрос уже передан администрации и ещё открыт. "
                                      "Повторно не отправляем; ответят сюда или свяжутся сами.")
        req = AdminRequest(telegram_id=self.user.telegram_id, kind=kind, summary=summary.strip()[:2000])
        self.db.add(req)
        await self.db.flush()
        try:
            req.admin_message_id = await self.notifier.admin_request(self.user, req, self.dialog_tail)
        except Exception:
            await self.db.delete(req)
            return ActionResult(False, "Не получилось передать администрации — технический сбой.")
        return ActionResult(True, "Передано администрации вместе с контекстом. Исход и срок не обещаем.")
