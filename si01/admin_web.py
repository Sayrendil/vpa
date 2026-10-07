"""Веб-админка: люди и их этапы, запросы людям, разбор ходов, события Tribute.

Живёт в том же aiohttp-сервере, что и вебхук Tribute, и пользуется тем же ботом: одобрение и сообщения
человеку уходят так же, как из админ-чата. Вход — по ADMIN_PANEL_PASSWORD, сессия в подписанной cookie.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from aiogram import Bot
from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, or_, select
from yarl import URL

from . import flows
from .config import Settings
from .db import AdminRequest, Database, Message, TributeEvent, TurnLog, User, aware, utcnow
from .facts import Facts
from .states import Source, Stage, can_transition
from .telegram import KIND_TITLES, LABELS, VERIFY_KINDS, TelegramOutbox, UserLocks

log = logging.getLogger(__name__)

COOKIE = "si01_admin"
SESSION_TTL = 7 * 24 * 3600
PER_PAGE = 50
# resolved_by для решений из панели: у неё нет telegram_id.
PANEL_ADMIN_ID = 0


def redirect(location: str) -> web.Response:
    return web.Response(status=302, headers={"Location": location})


class AdminPanel:
    def __init__(self, settings: Settings, database: Database, bot: Bot, facts: Facts, locks: UserLocks,
                 info: dict[str, str]):
        self.s, self.database, self.bot, self.facts, self.locks = settings, database, bot, facts, locks
        self.info = info
        self.secret = (settings.admin_panel_secret or
                       hashlib.sha256(f"si01-admin:{settings.admin_panel_password}".encode()).hexdigest()).encode()
        self.tz = timezone(timedelta(hours=settings.admin_panel_utc_offset))
        self.env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                               autoescape=select_autoescape(["html"]))
        self.env.filters["dt"] = self._fmt_dt
        self.env.filters["pretty"] = lambda v: json.dumps(v, ensure_ascii=False, indent=2)
        self.env.globals.update(KIND_TITLES=KIND_TITLES, STAGES=[s.value for s in Stage], LABELS=sorted(LABELS),
                                VERIFY_KINDS=VERIFY_KINDS, PANEL_ADMIN_ID=PANEL_ADMIN_ID)

    # ---------- сессия ----------

    def _sign(self, value: str) -> str:
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def _make_cookie(self) -> str:
        exp = str(int(time.time()) + SESSION_TTL)
        return f"{exp}.{self._sign(exp)}"

    def _cookie_ok(self, value: str | None) -> bool:
        if not value or "." not in value:
            return False
        exp, sig = value.split(".", 1)
        return hmac.compare_digest(self._sign(exp), sig) and exp.isdigit() and int(exp) > time.time()

    def _csrf(self, request: web.Request) -> str:
        return self._sign("csrf:" + (request.cookies.get(COOKIE) or ""))[:32]

    @web.middleware
    async def middleware(self, request: web.Request, handler):
        if not request.path.startswith("/admin") or request.path == "/admin/login":
            return await handler(request)
        if not self._cookie_ok(request.cookies.get(COOKIE)):
            raise web.HTTPFound("/admin/login")
        if request.method == "POST":
            form = await request.post()
            if not hmac.compare_digest(str(form.get("csrf", "")), self._csrf(request)):
                raise web.HTTPForbidden(text="CSRF")
        return await handler(request)

    # ---------- рендер ----------

    def _fmt_dt(self, dt: datetime | None) -> str:
        dt = aware(dt)
        return dt.astimezone(self.tz).strftime("%d.%m.%Y %H:%M") if dt else "—"

    def render(self, request: web.Request, template: str, **ctx) -> web.Response:
        html = self.env.get_template(template).render(
            request=request, csrf=self._csrf(request), info=self.info, path=request.path,
            flash=request.query.get("msg"), flash_error=request.query.get("err"), **ctx)
        return web.Response(text=html, content_type="text/html")

    @staticmethod
    def back(form, default: str, **params) -> web.HTTPFound:
        """Редирект туда, откуда пришла форма (поле next), с сообщением в ?msg= / ?err=."""
        target = str(form.get("next", "")) or default
        if not target.startswith("/admin/"):
            target = default
        url = URL(target)
        query = {k: v for k, v in url.query.items() if k not in ("msg", "err")} | params
        return web.HTTPFound(str(url.with_query(query)))

    @staticmethod
    def page_of(request: web.Request) -> int:
        p = request.query.get("page", "1")
        return max(int(p), 1) if p.isdigit() else 1

    @staticmethod
    def page_url(request: web.Request, page: int) -> str:
        q = dict(request.query)
        q.pop("msg", None), q.pop("err", None)
        q["page"] = str(page)
        return f"{request.path}?{urlencode(q)}"

    # ---------- вход ----------

    async def login_page(self, request: web.Request) -> web.Response:
        return self.render(request, "login.html", error=None)

    async def login(self, request: web.Request) -> web.Response:
        form = await request.post()
        if not hmac.compare_digest(str(form.get("password", "")).encode(), self.s.admin_panel_password.encode()):
            await asyncio.sleep(1)  # перебор паролей — помедленнее
            log.warning("admin panel: wrong password from %s", request.headers.get("X-Forwarded-For", request.remote))
            return self.render(request, "login.html", error="Неверный пароль")
        resp = redirect("/admin")
        secure = request.secure or request.headers.get("X-Forwarded-Proto") == "https"
        resp.set_cookie(COOKIE, self._make_cookie(), max_age=SESSION_TTL, httponly=True, samesite="Strict",
                        secure=secure, path="/admin")
        return resp

    async def logout(self, request: web.Request) -> web.Response:
        resp = redirect("/admin/login")
        resp.del_cookie(COOKIE, path="/admin")
        return resp

    # ---------- обзор ----------

    async def dashboard(self, request: web.Request) -> web.Response:
        day_ago = utcnow() - timedelta(days=1)
        async with self.database.session() as db:
            by_stage = dict((await db.execute(select(User.stage, func.count()).group_by(User.stage))).all())
            open_reqs = (await db.scalars(select(AdminRequest).where(AdminRequest.status == "open")
                                          .order_by(AdminRequest.created_at.desc()).limit(20))).all()
            users = await self._users_by_id(db, [r.telegram_id for r in open_reqs])
            turns_day = await db.scalar(select(func.count()).select_from(TurnLog).where(TurnLog.created_at >= day_ago))
            fallbacks_day = await db.scalar(select(func.count()).select_from(TurnLog).where(
                TurnLog.created_at >= day_ago, TurnLog.fallback_used.is_not(None)))
            users_day = await db.scalar(select(func.count()).select_from(User).where(User.created_at >= day_ago))
            labels = dict((await db.execute(select(TurnLog.label, func.count()).where(TurnLog.label.is_not(None))
                                            .group_by(TurnLog.label))).all())
        stages = [(s.value, by_stage.get(s.value, 0)) for s in Stage]
        return self.render(request, "dashboard.html", stages=stages, total=sum(by_stage.values()),
                           open_reqs=open_reqs, users=users, turns_day=turns_day, fallbacks_day=fallbacks_day,
                           users_day=users_day, labels=sorted(labels.items(), key=lambda kv: -kv[1]))

    @staticmethod
    async def _users_by_id(db, ids) -> dict[int, User]:
        ids = set(ids)
        if not ids:
            return {}
        return {u.telegram_id: u for u in await db.scalars(select(User).where(User.telegram_id.in_(ids)))}

    # ---------- люди ----------

    async def users(self, request: web.Request) -> web.Response:
        stage, q, page = request.query.get("stage", ""), request.query.get("q", "").strip(), self.page_of(request)
        stmt = select(User)
        if stage:
            stmt = stmt.where(User.stage == stage)
        if q:
            like = f"%{q.lstrip('@').lower()}%"
            conds = [func.lower(User.username).like(like), func.lower(User.first_name).like(like)]
            if q.isdigit():
                conds.append(User.telegram_id == int(q))
            stmt = stmt.where(or_(*conds))
        stmt = stmt.order_by(User.last_activity_at.desc().nulls_last(), User.created_at.desc())
        async with self.database.session() as db:
            rows = (await db.scalars(stmt.offset((page - 1) * PER_PAGE).limit(PER_PAGE + 1))).all()
        return self.render(request, "users.html", users=rows[:PER_PAGE], stage=stage, q=q, page=page,
                           has_next=len(rows) > PER_PAGE, page_url=lambda p: self.page_url(request, p))

    async def user(self, request: web.Request) -> web.Response:
        uid = int(request.match_info["uid"])
        async with self.database.session() as db:
            user = await db.get(User, uid)
            if not user:
                raise web.HTTPNotFound(text="Нет такого пользователя")
            messages = (await db.scalars(select(Message).where(Message.telegram_id == uid)
                                         .order_by(Message.created_at.desc(), Message.id.desc()).limit(300))).all()
            reqs = (await db.scalars(select(AdminRequest).where(AdminRequest.telegram_id == uid)
                                     .order_by(AdminRequest.created_at.desc()))).all()
            turns = (await db.scalars(select(TurnLog).where(TurnLog.telegram_id == uid)
                                      .order_by(TurnLog.created_at.desc()).limit(50))).all()
            events = (await db.scalars(select(TributeEvent).where(TributeEvent.telegram_id == uid)
                                       .order_by(TributeEvent.created_at.desc()))).all()
        return self.render(request, "user.html", u=user, messages=list(reversed(messages)), reqs=reqs, turns=turns,
                           events=events,
                           can_approve=can_transition(user.stage, Stage.PAYMENT_PENDING, Source.ADMIN),
                           can_reject=can_transition(user.stage, Stage.REJECTED, Source.ADMIN))

    async def decide(self, request: web.Request) -> web.Response:
        uid = int(request.match_info["uid"])
        form = await request.post()
        approve = form.get("decision") == "approve"
        default = f"/admin/users/{uid}"
        async with self.locks(uid), self.database.session() as db:
            user = await db.get(User, uid)
            if not user:
                raise web.HTTPNotFound()
            req = await db.scalar(select(AdminRequest).where(
                AdminRequest.telegram_id == uid, AdminRequest.status == "open", AdminRequest.kind.in_(VERIFY_KINDS))
                .order_by(AdminRequest.created_at.desc()))
            out = TelegramOutbox(self.bot, self.s, self.facts)
            stage_before = user.stage
            try:
                note = await flows.decide(db, out, user, approve=approve, admin_id=PANEL_ADMIN_ID, req=req)
            except Exception:
                log.exception("admin panel: decide failed for %s", uid)
                await db.rollback()
                raise self.back(form, default, err="Не удалось отправить человеку сообщение — решение не сохранено")
            done = user.stage != stage_before
            await db.commit()
            await out.flush()
        if not done:
            raise self.back(form, default, err=note)
        await self._mark_card(req, f"{note} — через админ-панель")
        if not req:
            await self._admin_chat(f"{note}: {uid} (@{user.username or '—'}) — через админ-панель")
        raise self.back(form, default, msg=note)

    async def send_message(self, request: web.Request) -> web.Response:
        uid = int(request.match_info["uid"])
        form = await request.post()
        text = str(form.get("text", "")).strip()
        default = f"/admin/users/{uid}"
        if not text:
            raise self.back(form, default, err="Пустое сообщение")
        full = f"Сообщение от администрации VPA:\n\n{text}"
        async with self.locks(uid), self.database.session() as db:
            user = await db.get(User, uid)
            if not user:
                raise web.HTTPNotFound()
            try:
                await self.bot.send_message(uid, full)
            except Exception as e:
                log.warning("admin panel: cannot message %s: %s", uid, e)
                raise self.back(form, default, err=f"Telegram не доставил: {e}")
            await flows.log_assistant(db, user, full)
            await db.commit()
        raise self.back(form, default, msg="📨 Отправлено")

    # ---------- запросы ----------

    async def requests(self, request: web.Request) -> web.Response:
        status, kind, page = request.query.get("status", "open"), request.query.get("kind", ""), self.page_of(request)
        stmt = select(AdminRequest)
        if status != "all":
            stmt = stmt.where(AdminRequest.status == status)
        if kind:
            stmt = stmt.where(AdminRequest.kind == kind)
        stmt = stmt.order_by(AdminRequest.created_at.desc()).offset((page - 1) * PER_PAGE).limit(PER_PAGE + 1)
        async with self.database.session() as db:
            rows = (await db.scalars(stmt)).all()
            users = await self._users_by_id(db, [r.telegram_id for r in rows])
        return self.render(request, "requests.html", reqs=rows[:PER_PAGE], users=users, status=status, kind=kind,
                           kinds=KIND_TITLES, page=page, has_next=len(rows) > PER_PAGE,
                           page_url=lambda p: self.page_url(request, p))

    async def close_request(self, request: web.Request) -> web.Response:
        rid = int(request.match_info["rid"])
        form = await request.post()
        async with self.database.session() as db:
            req = await db.get(AdminRequest, rid)
            if not req or req.status != "open":
                raise self.back(form, "/admin/requests", err="Запрос уже закрыт")
            req.status, req.resolved_by, req.resolved_at = "closed", PANEL_ADMIN_ID, utcnow()
            await db.commit()
        await self._mark_card(req, "✔️ Закрыто через админ-панель")
        raise self.back(form, "/admin/requests", msg=f"Запрос #{rid} закрыт")

    async def _mark_card(self, req: AdminRequest | None, note: str) -> None:
        """Убрать кнопки с карточки в админ-чате и ответить на неё, чтобы в чате было видно решение."""
        if not req or not req.admin_message_id or not self.s.admin_chat_id:
            return
        try:
            await self.bot.edit_message_reply_markup(chat_id=self.s.admin_chat_id, message_id=req.admin_message_id,
                                                     reply_markup=None)
        except Exception:
            pass
        await self._admin_chat(note, reply_to=req.admin_message_id)

    async def _admin_chat(self, text: str, reply_to: int | None = None) -> None:
        if not self.s.admin_chat_id:
            return
        try:
            await self.bot.send_message(self.s.admin_chat_id, text, reply_to_message_id=reply_to)
        except Exception:
            log.exception("admin panel: cannot post to admin chat")

    # ---------- ходы ----------

    async def turns(self, request: web.Request) -> web.Response:
        label, page = request.query.get("label", ""), self.page_of(request)
        uid = request.query.get("user", "")
        stmt = select(TurnLog)
        if label == "_none":
            stmt = stmt.where(TurnLog.label.is_(None))
        elif label == "_any":
            stmt = stmt.where(TurnLog.label.is_not(None))
        elif label:
            stmt = stmt.where(TurnLog.label == label)
        if request.query.get("fallback"):
            stmt = stmt.where(TurnLog.fallback_used.is_not(None))
        if uid.isdigit():
            stmt = stmt.where(TurnLog.telegram_id == int(uid))
        stmt = stmt.order_by(TurnLog.created_at.desc()).offset((page - 1) * PER_PAGE).limit(PER_PAGE + 1)
        async with self.database.session() as db:
            rows = (await db.scalars(stmt)).all()
            users = await self._users_by_id(db, [t.telegram_id for t in rows])
        return self.render(request, "turns.html", turns=rows[:PER_PAGE], users=users, label=label, uid=uid,
                           fallback=bool(request.query.get("fallback")), page=page, has_next=len(rows) > PER_PAGE,
                           page_url=lambda p: self.page_url(request, p))

    async def label_turn(self, request: web.Request) -> web.Response:
        tid = int(request.match_info["tid"])
        form = await request.post()
        label = str(form.get("label", "")).upper()
        async with self.database.session() as db:
            turn = await db.get(TurnLog, tid)
            if not turn:
                raise web.HTTPNotFound()
            if label and label not in LABELS:
                raise self.back(form, "/admin/turns", err="Неизвестная метка")
            turn.label = label or None
            turn.label_note = str(form.get("note", "")).strip() or None
            await db.commit()
        raise self.back(form, "/admin/turns", msg=f"🏷 #{tid}: {label or 'метка снята'}")

    # ---------- Tribute ----------

    async def tribute(self, request: web.Request) -> web.Response:
        page = self.page_of(request)
        async with self.database.session() as db:
            rows = (await db.scalars(select(TributeEvent).order_by(TributeEvent.created_at.desc())
                                     .offset((page - 1) * PER_PAGE).limit(PER_PAGE + 1))).all()
            users = await self._users_by_id(db, [e.telegram_id for e in rows if e.telegram_id])
        return self.render(request, "tribute.html", events=rows[:PER_PAGE], users=users, page=page,
                           has_next=len(rows) > PER_PAGE, page_url=lambda p: self.page_url(request, p))


def setup_admin(app: web.Application, settings: Settings, database: Database, bot: Bot, facts: Facts,
                locks: UserLocks, info: dict[str, str]) -> AdminPanel:
    panel = AdminPanel(settings, database, bot, facts, locks, info)
    app.middlewares.append(panel.middleware)

    async def to_admin(_: web.Request):
        raise web.HTTPFound("/admin")

    app.router.add_get("/", to_admin)
    app.router.add_get("/admin/login", panel.login_page)
    app.router.add_post("/admin/login", panel.login)
    app.router.add_post("/admin/logout", panel.logout)
    app.router.add_get("/admin", panel.dashboard)
    app.router.add_get("/admin/users", panel.users)
    app.router.add_get("/admin/users/{uid:\\d+}", panel.user)
    app.router.add_post("/admin/users/{uid:\\d+}/decide", panel.decide)
    app.router.add_post("/admin/users/{uid:\\d+}/message", panel.send_message)
    app.router.add_get("/admin/requests", panel.requests)
    app.router.add_post("/admin/requests/{rid:\\d+}/close", panel.close_request)
    app.router.add_get("/admin/turns", panel.turns)
    app.router.add_post("/admin/turns/{tid:\\d+}/label", panel.label_turn)
    app.router.add_get("/admin/tribute", panel.tribute)
    return panel

