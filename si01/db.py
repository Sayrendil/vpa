"""Хранилище: пользователь и его этап, история сессии, лог ходов, запросы в админ-чат, события Tribute."""

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, Text, ForeignKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite отдаёт naive datetime — считаем его UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}

    def __init__(self, **kw):
        # Скалярные default колонок применяем сразу, а не только при INSERT:
        # иначе у свежего User до flush stage=None.
        for col in self.__table__.columns:
            if col.key not in kw and col.default is not None and col.default.is_scalar:
                kw[col.key] = col.default.arg
        for k, v in kw.items():
            setattr(self, k, v)


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))

    # Системные состояния — НЕ сбрасываются вместе с сессией.
    stage: Mapped[str] = mapped_column(String(32), default="NEW")
    verification_status: Mapped[str] = mapped_column(String(32), default="none")
    payment_status: Mapped[str] = mapped_column(String(32), default="none")
    membership_status: Mapped[str] = mapped_column(String(32), default="none")
    membership_expires_at: Mapped[datetime | None]

    # Разговорная сессия — сбрасывается после SESSION_IDLE_HOURS тишины.
    session_no: Mapped[int] = mapped_column(Integer, default=1)
    session_summary: Mapped[str] = mapped_column(Text, default="")
    last_activity_at: Mapped[datetime | None]

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    session_no: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    summarized: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class TurnLog(Base):
    """Один ход для разбора качества: что пришло, в каком состоянии, что ответили и почему."""

    __tablename__ = "turn_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    session_no: Mapped[int] = mapped_column(Integer)
    stage_before: Mapped[str] = mapped_column(String(32))
    stage_after: Mapped[str] = mapped_column(String(32))
    user_text: Mapped[str] = mapped_column(Text)
    reply_text: Mapped[str] = mapped_column(Text)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    facts_version: Mapped[str] = mapped_column(String(32))
    fallback_used: Mapped[str | None] = mapped_column(String(64))
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    # id сообщения бота в Telegram — чтобы ответом «/label GOOD …» пометить именно этот ход.
    reply_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # Ручная метка Артура: GOOD / TOO_ROBOTIC / TOO_SALESY / QUESTIONNAIRE / INVENTED_FACT / ...
    label: Mapped[str | None] = mapped_column(String(32))
    label_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class AdminRequest(Base):
    """Всё, что SI-01 передаёт людям: проверка, созвон, «позови живого», апелляция, перенос оплаты…"""

    __tablename__ = "admin_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | approved | rejected | closed
    summary: Mapped[str] = mapped_column(Text, default="")
    admin_message_id: Mapped[int | None] = mapped_column(BigInteger)
    resolved_by: Mapped[int | None] = mapped_column(BigInteger)
    resolved_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class TributeEvent(Base):
    __tablename__ = "tribute_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sha256 тела — чтобы повторную доставку того же вебхука не обрабатывать дважды.
    body_hash: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    subscription_id: Mapped[int | None] = mapped_column(Integer)
    channel_id: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None]
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(url)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def session(self) -> AsyncSession:
        return self.sessionmaker()

    async def dispose(self) -> None:
        await self.engine.dispose()
