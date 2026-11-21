"""Database tables: conversations, their messages, and the settings the team can change."""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, false, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything in the app is UTC-aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class Role(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    STAFF = "staff"


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wa_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(20))
    name: Mapped[str | None] = mapped_column(String(120))
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Until this moment the chat belongs to the team and the agent stays out of it.
    team_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # First customer message the team has not answered yet, while the chat belongs to the team.
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Set when the guest asked for a person (or the agent could not help).
    attention_reason: Mapped[str | None] = mapped_column(String(300))
    last_customer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="(Message.created_at, Message.id)",
    )

    def team_active(self, now: datetime | None = None) -> bool:
        until = as_utc(self.team_until)
        return until is not None and until > (now or utcnow())


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    # WhatsApp's id for the message; unique, so a redelivered webhook is stored only once.
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
