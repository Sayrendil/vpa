"""Прогон EVAL_SET против настоящей модели (тратит токены!).

    python -m evals.run_evals                 # все сценарии
    python -m evals.run_evals 29_price 39     # только с этими префиксами id

Telegram не нужен: действия пишутся в фейковый outbox. Каждый сценарий — чистая БД в памяти.
Отчёт: evals/results/<время>.md и .json (prompt_version + facts_version внутри).
"""

import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import openai
import yaml
from pydantic import BaseModel

from si01.brain import Brain
from si01.config import get_settings
from si01.db import Database, User
from si01.facts import load_facts

HERE = Path(__file__).parent

STAGE_STATUSES = {
    "VERIFICATION_PENDING": dict(verification_status="awaiting_material"),
    "VERIFICATION_REVIEW": dict(verification_status="under_review"),
    "REJECTED": dict(verification_status="rejected"),
    "PAYMENT_PENDING": dict(verification_status="approved", payment_status="link_sent"),
    "MEMBER": dict(verification_status="approved", payment_status="paid", membership_status="active"),
}


class Verdict(BaseModel):
    passed: bool
    reason: str


class Recorder:
    def __init__(self):
        self.events = []

    async def admin_request(self, user, req, dialog_tail, reply_to=None):
        self.events.append(("admin_request", req.kind))
        return 1

    async def send_payment_link(self, user):
        self.events.append(("payment_link", user.telegram_id))


async def judge(client, model: str, dialog: str, expect: str) -> Verdict:
    resp = await client.responses.parse(
        model=model, max_output_tokens=8000, reasoning={"effort": "medium"}, store=False,
        input=[{"role": "user", "content": (
            "Ты проверяешь ответы Telegram-бота SI-01 (встречающий AI-помощник онлайн-пространства VPA). "
            "Оцени ПОСЛЕДНИЙ ответ бота в диалоге по критерию. Будь строгим, но не придирайся к формулировкам: "
            "важно поведение, а не конкретные слова. Проверяй ровно то, что написано в критерии, и не "
            "добавляй своих требований. Строки [действие: …] — то, что бот сделал через систему (например, "
            "система сама присылает кнопку со ссылкой на оплату); их тоже учитывай. "
            "reason — одно короткое предложение по-русски.\n\n"
            f"Критерий: {expect}\n\nДиалог:\n{dialog}"
        )}],
        text_format=Verdict,
    )
    return resp.output_parsed or Verdict(passed=False, reason=f"судья не ответил ({resp.status})")


async def run_scenario(sc: dict, brain: Brain, client, s) -> dict:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    rec = Recorder()
    replies, tools, turn_tools, problems = [], [], [], []
    stage = sc.get("stage", "EXPLORING")
    async with database.session() as db:
        user = User(telegram_id=1, first_name="Тест", stage=stage, **STAGE_STATUSES.get(stage, {}))
        if stage.startswith("MEMBER"):
            user.membership_expires_at = datetime.now(timezone.utc) + timedelta(days=20)
        db.add(user)
        await db.flush()
        for text in sc["turns"]:
            res = await brain.handle(db, user, text, rec)
            replies.append(res.text)
            names = [a.name + ("" if a.ok else " — не удалось") for a in res.actions]
            turn_tools.append(names)
            tools += [a.name for a in res.actions]
        stage_after = user.stage
    await database.dispose()

    for pat in sc.get("must_not", []):
        for r in replies:
            if re.search(pat, r):
                problems.append(f"запрещённый паттерн /{pat}/")
    for t in sc.get("tools_forbid", []):
        if t in tools:
            problems.append(f"вызвал запрещённое действие {t}")
    for t in sc.get("tools_expect", []):
        if t not in tools:
            problems.append(f"не вызвал {t}")
    if sc.get("stage_after") and stage_after != sc["stage_after"]:
        problems.append(f"этап {stage_after}, ожидался {sc['stage_after']}")

    dialog = "\n".join(
        f"Человек: {u}\n" + "".join(f"[действие: {n}]\n" for n in ts) + f"SI-01: {r}"
        for u, r, ts in zip(sc["turns"], replies, turn_tools)
    )
    # Судья — основная модель: aux-модель с низким effort путала критерии.
    verdict = await judge(client, s.llm_model, dialog, sc["expect"])
    if not verdict.passed:
        problems.append(f"судья: {verdict.reason}")
    return {"id": sc["id"], "critical": sc.get("critical", False), "passed": not problems,
            "problems": problems, "judge": verdict.reason, "dialog": dialog, "tools": tools,
            "stage_after": stage_after}


async def main(filters: list[str]) -> int:
    s = get_settings()
    facts = load_facts(s.facts_path)
    client = openai.AsyncOpenAI(api_key=s.openai_api_key or None)
    brain = Brain(s, facts, client)
    scenarios = yaml.safe_load((HERE / "scenarios.yaml").read_text(encoding="utf-8"))["scenarios"]
    if filters:
        scenarios = [sc for sc in scenarios if any(sc["id"].startswith(f) for f in filters)]

    sem = asyncio.Semaphore(4)

    async def guarded(sc):
        async with sem:
            r = await run_scenario(sc, brain, client, s)
            print(f"{'✅' if r['passed'] else ('🛑' if r['critical'] else '❌')} {r['id']}"
                  + ("" if r["passed"] else f" — {'; '.join(r['problems'])}"), flush=True)
            return r

    results = await asyncio.gather(*(guarded(sc) for sc in scenarios))
    passed = sum(r["passed"] for r in results)
    crit = [r["id"] for r in results if r["critical"] and not r["passed"]]

    out = HERE / "results"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    meta = {"model": s.llm_model, "effort": s.llm_effort, "prompt_version": brain.prompt_version,
            "facts_version": facts.version, "passed": passed, "total": len(results), "critical_failed": crit}
    (out / f"{stamp}.json").write_text(json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=2))
    md = [f"# Eval {stamp}", "", "```", json.dumps(meta, ensure_ascii=False, indent=1), "```", ""]
    for r in sorted(results, key=lambda r: (r["passed"], not r["critical"])):
        mark = "✅" if r["passed"] else ("🛑" if r["critical"] else "❌")
        md += [f"## {mark} {r['id']}", "", *(f"- {p}" for p in r["problems"]),
               f"- действия: {r['tools'] or '—'}; этап после: {r['stage_after']}", "", "```", r["dialog"], "```", ""]
    (out / f"{stamp}.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\n{passed}/{len(results)} прошли; критических провалов: {len(crit)} {crit or ''}\nОтчёт: {out / stamp}.md")
    return 1 if crit else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
