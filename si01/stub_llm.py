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


def _response(text=None, calls=()):
    output = [_ns(type="function_call", call_id=c_id, name=name, arguments=json.dumps(args, ensure_ascii=False))
              for c_id, name, args in calls]
    if text is not None:
        output.append(_ns(type="message", content=[_ns(type="output_text", text=text)]))
    return _ns(model="stub", usage=None, incomplete_details=None, output=output, output_text=text or "")


def _text(t):
    return _response(TAG + t)


def _tool(name, args):
    return _response(calls=[("stub1", name, args)])


class StubClient:
    def __init__(self):
        self.responses = _ns(create=self._create)

    async def _create(self, input, tools=None, **kw):
        messages = input
        if not tools:
            return _response("(тестовый режим: без краткого содержания)")
        last = messages[-1]
        # Второй круг: модель «получила» результат действия — пересказываем его.
        if last.get("type") == "function_call_output":
            res = last["output"]
            return _text(res if res.startswith("ОШИБКА") else "Сделано: " + res)

        user_msgs = [m for m in messages if m.get("role") == "user"]
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
