import json
from types import SimpleNamespace

import pytest

from si01.config import Settings
from si01.db import Database, User
from si01.facts import load_facts


class FakeOutbox:
    """Notifier + Outbox, который просто записывает, что ушло наружу."""

    def __init__(self, fail: bool = False):
        self.admin_requests, self.links, self.texts, self.admin_texts = [], [], [], []
        self.fail = fail

    async def admin_request(self, user, req, dialog_tail, reply_to=None):
        if self.fail:
            raise RuntimeError("telegram down")
        self.admin_requests.append((user.telegram_id, req.kind, req.summary))
        return 1000 + len(self.admin_requests)

    async def send_payment_link(self, user):
        self.links.append(user.telegram_id)

    async def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))

    async def admin_text(self, text):
        self.admin_texts.append(text)


def tool_call(call_id, name, args):
    return SimpleNamespace(type="function_call", call_id=call_id, name=name, arguments=json.dumps(args))


def response(text=None, tool_calls=()):
    output = list(tool_calls)
    if text is not None:
        output.append(SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text=text)]))
    return SimpleNamespace(
        model="gpt-5.5", output=output, output_text=text or "", incomplete_details=None,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                              input_tokens_details=SimpleNamespace(cached_tokens=0),
                              output_tokens_details=SimpleNamespace(reasoning_tokens=0)),
    )


class FakeClient:
    """Подставные ответы модели по очереди; запоминает, что ей отправили."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.calls.append({**kw, "input": list(kw.get("input", []))})
        return self.queue.pop(0)


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db",
                    tribute_api_key="secret", tribute_payment_url="https://t.me/tribute/app?startapp=x",
                    admin_chat_id=-100, admin_ids=[1])


@pytest.fixture
def facts(settings):
    return load_facts(settings.facts_path)


@pytest.fixture
async def database(settings):
    d = Database(settings.database_url)
    await d.create_all()
    yield d
    await d.dispose()


@pytest.fixture
async def db(database):
    async with database.session() as s:
        yield s


@pytest.fixture
async def user(db):
    u = User(telegram_id=42, username="vanya", first_name="Ваня")
    db.add(u)
    await db.flush()
    return u
