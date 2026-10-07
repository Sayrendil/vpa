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


def tool_call(id_, name, args):
    return SimpleNamespace(id=id_, type="function",
                           function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def response(text=None, tool_calls=None):
    msg = SimpleNamespace(content=text, refusal=None, tool_calls=tool_calls)
    return SimpleNamespace(
        model="gpt-5.5",
        choices=[SimpleNamespace(message=msg, finish_reason="tool_calls" if tool_calls else "stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=0)),
    )


class FakeClient:
    """Подставные ответы модели по очереди; запоминает, что ей отправили."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append({**kw, "messages": list(kw.get("messages", []))})
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
