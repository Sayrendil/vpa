"""Заглушка вместо Claude — когда ANTHROPIC_API_KEY пуст.

Нужна, чтобы проверить Telegram, админ-чат, проверку и этапы без платного API.
Отвечает по ключевым словам и вызывает те же действия, что и настоящая модель.
Характер и качество разговора так проверить нельзя.
"""

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


def _response(content, stop):
    return _ns(content=content, stop_reason=stop, stop_details=None, model="stub",
               usage=_ns(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                         cache_creation_input_tokens=0, iterations=None))


def _text(t):
    return _ns(type="text", text=TAG + t)


class StubClient:
    def __init__(self):
        self.beta = _ns(messages=_ns(create=self._create))
        self.messages = _ns(create=self._summary)

    async def _summary(self, **kw):
        return _response([_ns(type="text", text="(тестовый режим: без краткого содержания)")], "end_turn")

    async def _create(self, messages, **kw):
        last = messages[-1]
        # Второй круг: модель «получила» результат действия — пересказываем его.
        if last["role"] == "user" and isinstance(last["content"], list):
            res = last["content"][0]
            prefix = "Сделано: " if not res["is_error"] else "Не получилось: "
            return _response([_text(prefix + res["content"])], "end_turn")

        user_msgs = [m for m in messages if m["role"] == "user"]
        text = user_msgs[-1]["content"].split("\n\n")[-1].lower() if user_msgs else ""
        stage = re.search(r"Этап человека: (\w+)", messages[-1]["content"])
        stage = stage.group(1) if stage else "?"

        for pattern, tool, args in RULES:
            if re.search(pattern, text):
                return _response([_ns(type="tool_use", id="stub1", name=tool, input=args)], "tool_use")
        if "сколько" in text or "цен" in text or "стоит" in text:
            return _response([_text("Цена берётся из facts.yaml — с настоящей моделью тут будет живой ответ.")], "end_turn")
        return _response([_text(f"Получил: «{text[:200]}». Твой этап: {stage}.\n"
                                "Ключевые слова: «хочу вступить», «созвон», «ссылка», «позови живого».")], "end_turn")
