"""Заглушка вместо OpenAI — когда OPENAI_API_KEY пуст.

Нужна, чтобы проверить Telegram, админ-чат, проверку и этапы без платного API.
Отвечает по ключевым словам и вызывает те же действия, что и настоящая модель.
Характер и качество разговора так проверить нельзя.
"""

import json
import re
from types import SimpleNamespace

TAG = "🧪 [тест без AI] "

RULES = [
    (r"хочу (вступить|зайти|попасть)|как вступить|вступлени", "begin_verification", {}),
    (r"созвон|звонок|позвон", "request_call_verification", {"note": "Просит проверку созвоном (тестовый режим)"}),
    (r"ссылк|оплат", "resend_payment_link", {}),
    (r"живого|человека|админ|артур", "notify_admins", {"kind": "human", "summary": "Просит живого человека (тестовый режим)"}),
]


def _ns(**kw):
    return SimpleNamespace(**kw)


def _response(content=None, tool_calls=None):
    msg = _ns(content=content, refusal=None, tool_calls=tool_calls)
    return _ns(model="stub", usage=None,
               choices=[_ns(message=msg, finish_reason="tool_calls" if tool_calls else "stop")])


def _text(t):
    return _response(TAG + t)


def _tool(name, args):
    return _response(tool_calls=[_ns(id="stub1", type="function",
                                     function=_ns(name=name, arguments=json.dumps(args, ensure_ascii=False)))])


class StubClient:
    def __init__(self):
        self.chat = _ns(completions=_ns(create=self._create))

    async def _create(self, messages, tools=None, **kw):
        if not tools:
            return _response("(тестовый режим: без краткого содержания)")
        last = messages[-1]
        # Второй круг: модель «получила» результат действия — пересказываем его.
        if last["role"] == "tool":
            res = last["content"]
            return _text(res if res.startswith("ОШИБКА") else "Сделано: " + res)

        user_msgs = [m for m in messages if m["role"] == "user"]
        text = user_msgs[-1]["content"].split("\n\n")[-1].lower() if user_msgs else ""
        stage = re.search(r"Этап человека: (\w+)", messages[-1]["content"])
        stage = stage.group(1) if stage else "?"

        for pattern, tool, args in RULES:
            if re.search(pattern, text):
                return _tool(tool, args)
        if "сколько" in text or "цен" in text or "стоит" in text:
            return _text("Цена берётся из facts.yaml — с настоящей моделью тут будет живой ответ.")
        return _text(f"Получил: «{text[:200]}». Твой этап: {stage}.\n"
                     "Ключевые слова: «хочу вступить», «созвон», «ссылка», «позови живого».")
