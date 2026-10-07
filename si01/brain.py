"""LLM Orchestrator: собирает контекст хода, гоняет цикл инструментов, пишет историю и лог."""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta

import anthropic
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .actions import TOOLS, ActionLog, Actions, Notifier
from .config import Settings
from .db import Message, TurnLog, User, aware, utcnow
from .facts import Facts
from .states import STAGE_INFO, Source, Stage, refresh_membership, transition

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOOL_ROUNDS = 4

ERROR_REPLY = "Сейчас у меня что-то сломалось и нормально ответить не могу. Напиши чуть позже, ладно?"
REFUSAL_REPLY = "Тут я завис) Можешь сформулировать по-другому?"



@dataclass
class TurnResult:
    text: str
    actions: list[ActionLog] = field(default_factory=list)
    stage_before: str = ""
    stage_after: str = ""
    turn_log: TurnLog | None = None


def load_prompt(path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"prompt_version:\s*([\w.\-]+)", text)
    return text, (m.group(1) if m else "unknown")


class Brain:
    def __init__(self, settings: Settings, facts: Facts, client: anthropic.AsyncAnthropic,
                 capabilities: dict[str, bool] | None = None):
        self.s = settings
        self.facts = facts
        self.client = client
        self.prompt, self.prompt_version = load_prompt(settings.prompt_path)
        self.capabilities = capabilities or {"voice": settings.stt_enabled}

    # ---------- сессия ----------

    async def touch_session(self, db: AsyncSession, user: User) -> None:
        """48 часов тишины -> новая разговорная сессия. Системные статусы не трогаем."""
        now = utcnow()
        last = aware(user.last_activity_at)
        if last and now - last > timedelta(hours=self.s.session_idle_hours):
            user.session_no += 1
            user.session_summary = ""
        user.last_activity_at = now
        refresh_membership(user, now)
        if user.stage == Stage.NEW:
            transition(user, Stage.EXPLORING, Source.SYSTEM)

    async def history(self, db: AsyncSession, user: User) -> list[Message]:
        rows = await db.scalars(
            select(Message)
            .where(Message.telegram_id == user.telegram_id, Message.session_no == user.session_no,
                   Message.summarized.is_(False))
            .order_by(Message.id)
        )
        return list(rows)

    async def add_message(self, db: AsyncSession, user: User, role: str, content: str) -> None:
        db.add(Message(telegram_id=user.telegram_id, session_no=user.session_no, role=role, content=content))

    # ---------- контекст ----------

    def runtime_context(self, user: User) -> str:
        st = Stage(user.stage)
        info = STAGE_INFO[st]
        now = utcnow()
        lines = [
            "[СЛУЖЕБНОЕ СООБЩЕНИЕ СИСТЕМЫ — не от пользователя]",
            f"Этап человека: {st.value} — {info.meaning}",
            f"Что дальше: {info.next_step}",
            f"verification_status={user.verification_status}; payment_status={user.payment_status}; "
            f"membership_status={user.membership_status}",
        ]
        exp = aware(user.membership_expires_at)
        if exp:
            lines.append(f"Оплаченный период по данным Tribute до: {exp:%d.%m.%Y}")
            if st == Stage.MEMBER and exp < now:
                lines.append("Срок прошёл, а события о продлении от Tribute нет. Tribute может ещё "
                             "повторять списание. Не утверждай, что доступ открыт или закрыт.")
        caps = ["текст"]
        caps.append("голосовые распознаются в текст" if self.capabilities.get("voice")
                    else "голосовые в обычном разговоре НЕ распознаются (только как материал проверки)")
        caps.append("фото и файлы НЕ просматриваются")
        lines.append("Что ты умеешь принимать: " + "; ".join(caps))
        if user.session_summary:
            lines.append(f"Краткое содержание более ранней части этого разговора: {user.session_summary}")
        lines.append(f"Сейчас: {now:%d.%m.%Y}")
        return "\n".join(lines)

    def build_messages(self, hist: list[Message], user_text: str, ctx: str) -> list[dict]:
        msgs: list[dict] = []
        for m in hist:
            if msgs and msgs[-1]["role"] == m.role:
                msgs[-1]["content"] += "\n\n" + m.content
            else:
                msgs.append({"role": m.role, "content": m.content})
        if msgs and msgs[0]["role"] == "assistant":
            # /start-приветствие было первым — API требует, чтобы история начиналась с user.
            msgs.insert(0, {"role": "user", "content": "/start"})
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"] += "\n\n" + user_text
        else:
            msgs.append({"role": "user", "content": user_text})
        msgs.append({"role": "system", "content": ctx})
        return msgs

    @staticmethod
    def dialog_tail(hist: list[Message], user_text: str, n: int = 8) -> str:
        tail = [(m.role, m.content) for m in hist[-n:]] + [("user", user_text)]
        return "\n".join(f"{'Человек' if r == 'user' else 'SI-01'}: {c[:400]}" for r, c in tail)

    # ---------- ход ----------

    async def handle(self, db: AsyncSession, user: User, user_text: str, notifier: Notifier) -> TurnResult:
        started = time.monotonic()
        await self.touch_session(db, user)
        stage_before = user.stage
        hist = await self.history(db, user)
        actions = Actions(db=db, user=user, notifier=notifier,
                          payment_url_configured=bool(self.s.tribute_payment_url),
                          dialog_tail=self.dialog_tail(hist, user_text))
        messages = self.build_messages(hist, user_text, self.runtime_context(user))

        text, fallback, usage = await self._run(messages, actions)

        await self.add_message(db, user, "user", user_text)
        if text not in (ERROR_REPLY, REFUSAL_REPLY):
            # Сбойные ответы в историю не пишем: следующий ход увидит вопрос человека без «ответа».
            await self.add_message(db, user, "assistant", text)
        turn = TurnLog(
            telegram_id=user.telegram_id, session_no=user.session_no,
            stage_before=stage_before, stage_after=user.stage,
            user_text=user_text, reply_text=text,
            actions=[a.__dict__ for a in actions.log],
            model=self.s.llm_model, prompt_version=self.prompt_version, facts_version=self.facts.version,
            fallback_used=fallback, usage=usage, latency_ms=int((time.monotonic() - started) * 1000),
        )
        db.add(turn)
        await db.flush()
        await self.maybe_summarize(db, user)
        return TurnResult(text, actions.log, stage_before, user.stage, turn)

    async def _run(self, messages: list[dict], actions: Actions) -> tuple[str, str | None, dict]:
        usage_total: dict[str, int] = {}
        fallback = None
        for _ in range(MAX_TOOL_ROUNDS + 1):
            try:
                resp = await self.client.beta.messages.create(
                    model=self.s.llm_model,
                    max_tokens=self.s.llm_max_tokens,
                    system=[
                        {"type": "text", "text": self.prompt},
                        {"type": "text", "text": self.facts.text, "cache_control": {"type": "ephemeral"}},
                    ],
                    messages=messages,
                    tools=TOOLS,
                    output_config={"effort": self.s.llm_effort},
                    cache_control={"type": "ephemeral"},
                    fallbacks="default",
                    betas=[FALLBACK_BETA],
                )
            except anthropic.RateLimitError:
                log.warning("LLM rate limited")
                return ERROR_REPLY, None, usage_total
            except anthropic.APIStatusError as e:
                log.error("LLM API error %s: %s", e.status_code, e.message)
                return ERROR_REPLY, None, usage_total
            except anthropic.APIConnectionError:
                log.error("LLM connection error")
                return ERROR_REPLY, None, usage_total

            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                usage_total[k] = usage_total.get(k, 0) + (getattr(resp.usage, k, 0) or 0)
            if any(getattr(it, "type", "") == "fallback_message" for it in (getattr(resp.usage, "iterations", None) or [])):
                fallback = resp.model

            if resp.stop_reason == "refusal":
                log.warning("LLM refusal: %s", resp.stop_details)
                return REFUSAL_REPLY, fallback, usage_total

            if resp.stop_reason != "tool_use":
                text = "".join(b.text for b in resp.content if b.type == "text").strip()
                return (text or "🙂"), fallback, usage_total

            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                args = block.input if isinstance(block.input, dict) else json.loads(block.input or "{}")
                res = await actions.run(block.name, args)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": res.message, "is_error": not res.ok})
            messages.append({"role": "user", "content": results})
        return ERROR_REPLY, fallback, usage_total

    # ---------- сжатие длинной истории ----------

    async def maybe_summarize(self, db: AsyncSession, user: User) -> None:
        hist = await self.history(db, user)
        if len(hist) <= self.s.history_window:
            return
        old = hist[: len(hist) - self.s.history_window // 2]
        transcript = "\n".join(f"{'Человек' if m.role == 'user' else 'SI-01'}: {m.content}" for m in old)
        prompt = (
            "Сожми начало переписки SI-01 (помощник VPA) с человеком в 3–8 коротких пунктов для "
            "продолжения разговора: что человек сам рассказал о себе, что спрашивал, что ему уже "
            "объяснили и что SI-01 обещал или передал людям. Только факты из текста, без оценок "
            "личности. Обычный текст, без markdown.\n\n"
            + (f"Предыдущее краткое содержание:\n{user.session_summary}\n\n" if user.session_summary else "")
            + f"Переписка:\n{transcript}"
        )
        try:
            resp = await self.client.messages.create(
                model=self.s.llm_aux_model, max_tokens=4000,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as e:
            log.warning("summary failed: %s", e)
            return
        if resp.stop_reason == "refusal":
            return
        summary = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not summary:
            return
        user.session_summary = summary
        await db.execute(update(Message).where(Message.id.in_([m.id for m in old])).values(summarized=True))
