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


def block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def response(*content, stop="end_turn"):
    return SimpleNamespace(content=list(content), stop_reason=stop, stop_details=None, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0, iterations=None))


class FakeClient:
    """Подставные ответы модели по очереди; запоминает, что ей отправили."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(create=self._create)

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
