"""Вебхуки Tribute: new_subscription / renewed_subscription / cancelled_subscription.

Подпись: заголовок trbt-signature = HMAC-SHA256(тело запроса, API-ключ Tribute), hex.
Tribute повторяет недоставленные вебхуки ~24 часа, поэтому обработка идемпотентна (по хэшу тела).
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime

from aiohttp import web
from sqlalchemy import select

from . import flows
from .config import Settings
from .db import Database, TributeEvent, User

log = logging.getLogger(__name__)

SUBSCRIPTION_EVENTS = {"new_subscription", "renewed_subscription", "cancelled_subscription"}


def verify_signature(body: bytes, signature: str | None, api_key: str) -> bool:
    if not signature or not api_key:
        return False
    expected = hmac.new(api_key.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


async def process_event(database: Database, outbox: flows.Outbox, settings: Settings, body: bytes) -> str:
    data = json.loads(body)
    name = data.get("name", "")
    payload = data.get("payload") or {}
    body_hash = hashlib.sha256(body).hexdigest()
    async with database.session() as db:
        if await db.scalar(select(TributeEvent.id).where(TributeEvent.body_hash == body_hash)):
            return "duplicate"
        tg_id = payload.get("telegram_user_id")
        expires = _parse_dt(payload.get("expires_at"))
        db.add(TributeEvent(body_hash=body_hash, name=name, telegram_id=tg_id,
                            subscription_id=payload.get("subscription_id"), channel_id=payload.get("channel_id"),
                            expires_at=expires, payload=data))
        relevant = name in SUBSCRIPTION_EVENTS and tg_id and (
            not settings.tribute_subscription_ids or payload.get("subscription_id") in settings.tribute_subscription_ids)
        if relevant:
            user = await db.get(User, tg_id)
            if user is None:
                user = User(telegram_id=tg_id, username=payload.get("telegram_username"))
                db.add(user)
                await db.flush()
            await flows.subscription_event(db, outbox, user, name, expires)
        await db.commit()
    return "ok" if relevant else "ignored"


def build_app(database: Database, outbox: flows.Outbox, settings: Settings) -> web.Application:
    async def handle(request: web.Request) -> web.Response:
        body = await request.read()
        if not verify_signature(body, request.headers.get("trbt-signature"), settings.tribute_api_key):
            log.warning("tribute webhook: bad signature")
            return web.json_response({"status": "invalid signature"}, status=401)
        try:
            result = await process_event(database, outbox, settings, body)
        except (ValueError, KeyError) as e:
            log.warning("tribute webhook: bad payload %s", e)
            return web.json_response({"status": "bad payload"}, status=400)
        log.info("tribute webhook: %s", result)
        return web.json_response({"status": "ok"})

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/webhook/tribute", handle)
    app.router.add_get("/health", health)
    return app
