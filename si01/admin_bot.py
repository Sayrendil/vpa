"""Админ-меню в личке с ботом: /admin — только для ADMIN_IDS, остальные его не видят.

Сводка, открытые запросы с решениями, поиск человека, сообщение от администрации, разметка ходов.
Навигация — правкой одного сообщения через inline-кнопки (callback_data «am:…»);
ввод текста (поиск, сообщение человеку) — через состояние FSM, «Отмена» или /cancel его сбрасывает.
Роутер подключается до обычного разговора, поэтому текст админа в режиме ввода не уходит модели.
"""

import html
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as TgMessage
from sqlalchemy import func, or_, select

from .admin_ops import AdminOps
from .db import AdminRequest, Message, TurnLog, User, aware
from .states import Source, Stage, can_transition
from .telegram import KIND_TITLES, LABELS, VERIFY_KINDS, who

# GOOD — самая частая метка, ставим её первой.
LABEL_ORDER = ["GOOD", *sorted(LABELS - {"GOOD"})]
LIMIT = 4000  # с запасом до 4096 символов сообщения Telegram


class AdminInput(StatesGroup):
    search = State()
    write = State()


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


MENU_ROW = [btn("← Меню", "am:home")]
CANCEL_KB = kb([btn("Отмена", "am:cancel")])


def cut(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def build_admin_menu(ops: AdminOps) -> Router:
    s = ops.s
    tz = timezone(timedelta(hours=s.admin_panel_utc_offset))
    r = Router()
    r.message.filter(F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(s.admin_ids))
    r.callback_query.filter(F.data.startswith("am:"), F.from_user.id.in_(s.admin_ids))

    def dt(v: datetime | None) -> str:
        v = aware(v)
        return v.astimezone(tz).strftime("%d.%m %H:%M") if v else "—"

    def short(u: User | None, uid: int) -> str:
        if not u:
            return f"id <code>{uid}</code>"
        return html.escape(u.first_name or "без имени") + (f" @{html.escape(u.username)}" if u.username else "")

    def via(cb: CallbackQuery) -> str:
        name = f"@{cb.from_user.username}" if cb.from_user.username else cb.from_user.full_name
        return f"через меню бота, {name}"

    async def show(target: CallbackQuery | TgMessage, text: str, markup: InlineKeyboardMarkup) -> None:
        """Из кнопки — правим то же сообщение, из команды/текста — шлём новое."""
        if isinstance(target, CallbackQuery):
            try:
                await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            except TelegramBadRequest as e:
                if "not modified" not in str(e):
                    raise
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=markup)

    # ---------- экраны ----------

    async def home_view():
        ov = await ops.overview()
        text = (f"<b>Админ-меню SI-01</b>\n\nОткрытых запросов: <b>{ov.open_count}</b>\n"
                f"Людей: {ov.total} (+{ov.users_day} за сутки)")
        return text, kb([btn("📊 Сводка", "am:stats"), btn(f"📥 Запросы ({ov.open_count})", "am:rq:0")],
                        [btn("🔎 Найти человека", "am:find"), btn("🏷 Разметка ходов", "am:lb")])

    async def stats_view():
        ov = await ops.overview()
        lines = ["<b>Сводка</b>", "", "<b>По этапам</b>"]
        lines += [f"{stage}: {n}" for stage, n in ov.stages if n]
        lines += ["", f"Всего людей: {ov.total} (+{ov.users_day} за сутки)", f"Открытых запросов: {ov.open_count}",
                  f"Ходов за сутки: {ov.turns_day}, из них фолбэков: {ov.fallbacks_day}"]
        if ov.labels:
            lines += ["", "<b>Метки ходов</b>"] + [f"{lb}: {n}" for lb, n in ov.labels]
        return "\n".join(lines), kb(MENU_ROW)

    async def request_view(i: int):
        async with ops.database.session() as db:
            n = await db.scalar(select(func.count()).select_from(AdminRequest).where(AdminRequest.status == "open"))
            if not n:
                return "Открытых запросов нет 🎉", kb(MENU_ROW)
            i = max(0, min(i, n - 1))
            req = await db.scalar(select(AdminRequest).where(AdminRequest.status == "open")
                                  .order_by(AdminRequest.created_at, AdminRequest.id).offset(i).limit(1))
            user = await db.get(User, req.telegram_id)
        uid = req.telegram_id
        parts = [f"<b>{KIND_TITLES.get(req.kind, req.kind)}</b> #{req.id} · {i + 1} из {n}",
                 f"От: {who(user) if user else short(None, uid)}",
                 f"Этап: {user.stage if user else '—'}", f"Создан: {dt(req.created_at)}"]
        if req.summary:
            parts.append(f"\n<b>Суть:</b> {html.escape(cut(req.summary, 2500))}")
        if req.kind in VERIFY_KINDS:
            parts.append("\n<i>Материал проверки — в админ-чате, над карточкой запроса.</i>")
            actions = [btn("✅ Одобрить", f"am:ok:{uid}:r{i}"), btn("❌ Отклонить", f"am:no:{uid}:r{i}")]
        else:
            actions = [btn("✔️ Закрыть", f"am:cl:{req.id}:{i}")]
        nav = ([btn("◀", f"am:rq:{i - 1}")] if i > 0 else []) + ([btn("▶", f"am:rq:{i + 1}")] if i < n - 1 else [])
        return "\n".join(parts), kb(actions, [btn("✉️ Ответить", f"am:wr:{uid}"), btn("👤 Профиль", f"am:u:{uid}")],
                                    nav, MENU_ROW)

    async def profile_view(uid: int):
        async with ops.database.session() as db:
            u = await db.get(User, uid)
            if not u:
                return f"Не знаю человека с id {uid}.", kb([btn("🔎 Найти", "am:find")], MENU_ROW)
            msgs = (await db.scalars(select(Message).where(Message.telegram_id == uid)
                                     .order_by(Message.created_at.desc(), Message.id.desc()).limit(6))).all()
        lines = [who(u), f"Этап: <b>{u.stage}</b>", f"Проверка: {u.verification_status}",
                 f"Оплата: {u.payment_status}",
                 f"Подписка: {u.membership_status}" + (f" до {dt(u.membership_expires_at)}" if u.membership_expires_at else ""),
                 f"Активность: {dt(u.last_activity_at)}"]
        if msgs:
            lines.append("\n<b>Последние сообщения</b>")
            for m in reversed(msgs):
                icon = "👤" if m.role == "user" else "🤖"
                lines.append(f"{icon} <i>{dt(m.created_at)}</i> {html.escape(cut(m.content, 400))}")
        decide = []
        if can_transition(u.stage, Stage.PAYMENT_PENDING, Source.ADMIN):
            decide.append(btn("✅ Одобрить", f"am:ok:{uid}:p"))
        if can_transition(u.stage, Stage.REJECTED, Source.ADMIN):
            decide.append(btn("❌ Отклонить", f"am:no:{uid}:p"))
        return cut("\n".join(lines), LIMIT), kb(decide, [btn("✉️ Написать", f"am:wr:{uid}")],
                                                [btn("🔎 Найти другого", "am:find"), MENU_ROW[0]])

    async def label_view(before: int | None):
        async with ops.database.session() as db:
            q = select(TurnLog).where(TurnLog.label.is_(None))
            left = await db.scalar(select(func.count()).select_from(TurnLog).where(TurnLog.label.is_(None)))
            if before:
                q = q.where(TurnLog.id < before)
            t = await db.scalar(q.order_by(TurnLog.id.desc()).limit(1))
            u = await db.get(User, t.telegram_id) if t else None
        if not t:
            return "Ходов без метки больше нет 🎉", kb(MENU_ROW)
        stage = t.stage_before + (f" → {t.stage_after}" if t.stage_after != t.stage_before else "")
        extra = "".join(f"\n⚙️ {html.escape(a.get('name', '?'))}" for a in (t.actions or []) if isinstance(a, dict))
        if t.fallback_used:
            extra += f"\n⚠️ фолбэк: {html.escape(t.fallback_used)}"
        text = (f"<b>Ход #{t.id}</b> · {dt(t.created_at)} · без метки: {left}\n{short(u, t.telegram_id)} · {stage}{extra}"
                f"\n\n👤 {html.escape(cut(t.user_text, 1500))}\n\n🤖 {html.escape(cut(t.reply_text, 1800))}")
        grid = [btn(lb, f"am:lt:{t.id}:{lb}") for lb in LABEL_ORDER]
        rows = [grid[i:i + 2] for i in range(0, len(grid), 2)]
        return cut(text, LIMIT), kb(*rows, [btn("⏭ Пропустить", f"am:lb:{t.id}"), MENU_ROW[0]])

    # ---------- команды ----------

    @r.message(Command("admin"))
    async def cmd_admin(msg: TgMessage, state: FSMContext):
        await state.clear()
        await show(msg, *await home_view())

    @r.message(Command("cancel"), StateFilter(AdminInput))
    async def cmd_cancel(msg: TgMessage, state: FSMContext):
        await state.clear()
        await show(msg, *await home_view())

    # ---------- кнопки ----------

    @r.callback_query(F.data == "am:home")
    async def on_home(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await show(cb, *await home_view())
        await cb.answer()

    @r.callback_query(F.data == "am:cancel")
    async def on_cancel(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await show(cb, *await home_view())
        await cb.answer("Отменено")

    @r.callback_query(F.data == "am:stats")
    async def on_stats(cb: CallbackQuery):
        await show(cb, *await stats_view())
        await cb.answer()

    @r.callback_query(F.data.startswith("am:rq:"))
    async def on_requests(cb: CallbackQuery):
        await show(cb, *await request_view(int(cb.data.split(":")[2])))
        await cb.answer()

    @r.callback_query(F.data.regexp(r"^am:(ok|no):\d+:(p|r\d+)$"))
    async def on_decide(cb: CallbackQuery):
        _, action, uid, back = cb.data.split(":")
        res = await ops.decide(int(uid), approve=action == "ok", admin_id=cb.from_user.id, via=via(cb))
        view = await (profile_view(int(uid)) if back == "p" else request_view(int(back[1:])))
        await show(cb, *view)
        await cb.answer(res.text, show_alert=not res.ok)

    @r.callback_query(F.data.startswith("am:cl:"))
    async def on_close(cb: CallbackQuery):
        _, _, rid, i = cb.data.split(":")
        res = await ops.close_request(int(rid), admin_id=cb.from_user.id, via=via(cb))
        await show(cb, *await request_view(int(i)))
        await cb.answer(res.text, show_alert=not res.ok)

    @r.callback_query(F.data.startswith("am:u:"))
    async def on_profile(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await show(cb, *await profile_view(int(cb.data.split(":")[2])))
        await cb.answer()

    @r.callback_query(F.data == "am:find")
    async def on_find(cb: CallbackQuery, state: FSMContext):
        await state.set_state(AdminInput.search)
        await show(cb, "🔎 Пришли telegram id, @username или имя.", CANCEL_KB)
        await cb.answer()

    @r.callback_query(F.data.startswith("am:wr:"))
    async def on_write(cb: CallbackQuery, state: FSMContext):
        uid = int(cb.data.split(":")[2])
        async with ops.database.session() as db:
            u = await db.get(User, uid)
        await state.set_state(AdminInput.write)
        await state.update_data(uid=uid)
        # Новым сообщением, чтобы карточка запроса/профиля осталась на месте.
        await cb.message.answer(f"✉️ Напиши сообщение для {short(u, uid)}.\n"
                                "Уйдёт как «Сообщение от администрации VPA» и попадёт в историю разговора.",
                                parse_mode="HTML", reply_markup=CANCEL_KB)
        await cb.answer()

    @r.callback_query(F.data == "am:lb")
    @r.callback_query(F.data.startswith("am:lb:"))
    async def on_label_next(cb: CallbackQuery):
        parts = cb.data.split(":")
        await show(cb, *await label_view(int(parts[2]) if len(parts) > 2 else None))
        await cb.answer()

    @r.callback_query(F.data.startswith("am:lt:"))
    async def on_label(cb: CallbackQuery):
        _, _, tid, label = cb.data.split(":")
        if label not in LABELS:
            return await cb.answer("Неизвестная метка", show_alert=True)
        async with ops.database.session() as db:
            turn = await db.get(TurnLog, int(tid))
            if turn:
                turn.label = label
                await db.commit()
        await show(cb, *await label_view(int(tid)))
        await cb.answer(f"🏷 #{tid}: {label}" if turn else "Ход уже удалён")

    # ---------- ввод текста ----------

    @r.message(AdminInput.search, F.text)
    async def on_search(msg: TgMessage, state: FSMContext):
        q = msg.text.strip()
        like = f"%{q.lstrip('@').lower()}%"
        conds = [func.lower(User.username).like(like), func.lower(User.first_name).like(like)]
        if q.isdigit():
            conds.append(User.telegram_id == int(q))
        async with ops.database.session() as db:
            found = (await db.scalars(select(User).where(or_(*conds))
                                      .order_by(User.last_activity_at.desc().nulls_last()).limit(11))).all()
        if not found:
            return await msg.answer(f"Не нашёл «{html.escape(q)}». Пришли ещё раз или нажми «Отмена».",
                                    parse_mode="HTML", reply_markup=CANCEL_KB)
        await state.clear()
        if len(found) == 1:
            return await show(msg, *await profile_view(found[0].telegram_id))
        rows = [[btn(f"{cut(u.first_name or 'без имени', 20)}{' @' + u.username if u.username else ''} · {u.stage}",
                     f"am:u:{u.telegram_id}")] for u in found[:10]]
        more = "\nПоказаны первые 10 — уточни запрос." if len(found) > 10 else ""
        await msg.answer(f"Нашёл несколько:{more}", reply_markup=kb(*rows, MENU_ROW))

    @r.message(AdminInput.write, F.text, ~F.text.startswith("/"))
    async def on_write_text(msg: TgMessage, state: FSMContext):
        uid = (await state.get_data())["uid"]
        res = await ops.message_user(uid, msg.text)
        if res.ok:
            await state.clear()
        await msg.answer(res.text, reply_markup=kb([btn("👤 Профиль", f"am:u:{uid}"), MENU_ROW[0]]))

    @r.message(StateFilter(AdminInput))
    async def on_not_text(msg: TgMessage):
        await msg.answer("Нужен текст. Или нажми «Отмена».", reply_markup=CANCEL_KB)

    return r
