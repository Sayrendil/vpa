"""Экспорт обезличенных диалогов для тестов (их можно хранить дольше 60 дней).

    python -m scripts.export_anonymized out.jsonl [--labeled]

Убирает Telegram ID, username, имя; известные имя/username в тексте заменяет на плейсхолдеры.
Свободный текст всё равно может содержать личные данные (город, работа, телефон) —
перед долгим хранением просмотри файл глазами.
"""

import asyncio
import json
import re
import sys

from sqlalchemy import select

from si01.config import get_settings
from si01.db import Database, TurnLog, User

PHONE = re.compile(r"\+?\d[\d\s\-()]{8,}\d")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
HANDLE = re.compile(r"@\w{4,}")


def scrub(text: str, user: User | None) -> str:
    if user:
        for val, ph in ((user.username, "<USERNAME>"), (user.first_name, "<ИМЯ>")):
            if val and len(val) > 1:
                text = re.sub(re.escape(val), ph, text, flags=re.IGNORECASE)
    text = PHONE.sub("<ТЕЛЕФОН>", text)
    text = EMAIL.sub("<EMAIL>", text)
    return HANDLE.sub("<USERNAME>", text)


async def main(path: str, labeled_only: bool) -> None:
    s = get_settings()
    database = Database(s.database_url)
    async with database.session() as db:
        q = select(TurnLog).order_by(TurnLog.telegram_id, TurnLog.id)
        if labeled_only:
            q = q.where(TurnLog.label.is_not(None))
        turns = list(await db.scalars(q))
        users = {u.telegram_id: u for u in await db.scalars(select(User))}
    conv_ids: dict[tuple[int, int], int] = {}
    with open(path, "w", encoding="utf-8") as f:
        for t in turns:
            conv = conv_ids.setdefault((t.telegram_id, t.session_no), len(conv_ids) + 1)
            u = users.get(t.telegram_id)
            f.write(json.dumps({
                "conversation": conv, "stage_before": t.stage_before, "stage_after": t.stage_after,
                "user": scrub(t.user_text, u), "reply": scrub(t.reply_text, u),
                "actions": [a["name"] for a in t.actions], "label": t.label,
                "label_note": scrub(t.label_note or "", u) or None,
                "prompt_version": t.prompt_version, "facts_version": t.facts_version,
            }, ensure_ascii=False) + "\n")
    await database.dispose()
    print(f"{len(turns)} ходов, {len(conv_ids)} разговоров -> {path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    asyncio.run(main(sys.argv[1], "--labeled" in sys.argv))
