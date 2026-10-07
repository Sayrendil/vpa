"""Запуск: python -m si01 — Telegram long polling + HTTP-сервер (вебхук Tribute, админка) + фоновые задачи."""

import asyncio
import logging
from datetime import timedelta

import anthropic
from aiogram import Bot, Dispatcher
from aiohttp import web
from sqlalchemy import delete, select

from .admin_web import setup_admin
from .brain import Brain
from .config import get_settings
from .db import AdminRequest, Database, Message, TurnLog, User, utcnow
from .facts import load_facts, load_greetings
from .states import Stage, refresh_membership
from .stt import SpeechToText
from .telegram import TelegramOutbox, UserLocks, build_router
from .tribute import setup_tribute

log = logging.getLogger("si01")


async def housekeeping(database: Database, retention_days: int) -> None:
    """Раз в час: истёкшие отменённые подписки -> EXPIRED; логи старше срока хранения -> удалить."""
    while True:
        try:
            async with database.session() as db:
                for user in await db.scalars(select(User).where(User.stage == Stage.MEMBER_CANCELLED)):
                    refresh_membership(user)
                cutoff = utcnow() - timedelta(days=retention_days)
                await db.execute(delete(Message).where(Message.created_at < cutoff))
                await db.execute(delete(TurnLog).where(TurnLog.created_at < cutoff))
                await db.execute(delete(AdminRequest).where(AdminRequest.created_at < cutoff,
                                                            AdminRequest.status != "open"))
                await db.commit()
        except Exception:
            log.exception("housekeeping failed")
        await asyncio.sleep(3600)


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    if not s.bot_token:
        raise SystemExit("Заполни BOT_TOKEN в .env")
    if not s.admin_chat_id or not s.admin_ids:
        log.warning("ADMIN_CHAT_ID/ADMIN_IDS пусты: проверки и запросы людям работать не будут. "
                    "Добавь бота в админ-чат и напиши там /chatid.")

    database = Database(s.database_url)
    await database.create_all()
    facts = load_facts(s.facts_path)
    greetings = load_greetings(s.greetings_path)
    if s.anthropic_api_key:
        client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
    else:
        from .stub_llm import StubClient
        log.warning("ANTHROPIC_API_KEY пуст — ТЕСТОВЫЙ РЕЖИМ БЕЗ AI: ответы по ключевым словам")
        client = StubClient()
    brain = Brain(s, facts, client)
    stt = SpeechToText(s.stt_base_url, s.stt_api_key, s.stt_model) if s.stt_enabled else None
    log.info("SI-01: model=%s prompt=%s facts=%s voice=%s", s.llm_model, brain.prompt_version,
             facts.version, bool(stt))

    bot = Bot(s.bot_token)
    dp = Dispatcher()
    locks = UserLocks()  # общие для бота и админки: решение из панели не пересекается с ходом человека
    dp.include_router(build_router(s, database, brain, facts, greetings, stt, locks))

    app = web.Application()
    app.router.add_get("/health", health)
    if s.tribute_api_key:
        setup_tribute(app, database, TelegramOutbox(bot, s, facts), s)
        log.info("Tribute webhook: /webhook/tribute")
    else:
        log.warning("TRIBUTE_API_KEY пуст — вебхуки Tribute выключены")
    if s.admin_panel_password:
        setup_admin(app, s, database, bot, facts, locks, info={
            "Модель": s.llm_model if s.anthropic_api_key else "ТЕСТОВЫЙ РЕЖИМ БЕЗ AI",
            "Промпт": brain.prompt_version, "Факты": facts.version,
            "Голос": "вкл" if stt else "выкл", "Tribute": "вкл" if s.tribute_api_key else "выкл",
            "Хранение логов": f"{s.log_retention_days} дн.", "Сессия": f"{s.session_idle_hours} ч тишины",
        })
        log.info("Админка: /admin")
    else:
        log.warning("ADMIN_PANEL_PASSWORD пуст — веб-админка выключена")
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, s.webhook_host, s.webhook_port).start()
    log.info("HTTP: http://%s:%s", s.webhook_host, s.webhook_port)

    hk = asyncio.create_task(housekeeping(database, s.log_retention_days))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        hk.cancel()
        await runner.cleanup()
        await bot.session.close()
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
