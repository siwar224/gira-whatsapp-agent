"""The rules of every conversation.

* The agent answers each guest message straight away.
* As soon as someone from the team writes in a chat (from the WhatsApp Business app or from the
  admin inbox) the chat is theirs and the agent stays out of it for `takeover_hours`.
* Safety net: in a chat the team took over, a guest message left unanswered for
  `reply_after_minutes` is answered by the agent.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.agent.runner import SORRY, AgentReply, AgentUnavailable, Responder, Turn
from app.agent.tools import GuestContext
from app.business.phone import from_whatsapp_id
from app.core.logging import mask_phone
from app.db.models import Conversation, Message, Role, as_utc, utcnow
from app.services.runtime_settings import RuntimeSettingsStore
from app.whatsapp.client import MessageSender, WhatsAppError
from app.whatsapp.events import InboundMessage, TeamMessage

log = logging.getLogger(__name__)

HISTORY_LIMIT = 40
STALE_AFTER = timedelta(minutes=15)  # redelivered or backlog messages are stored, not answered
SERVICE_WINDOW = timedelta(hours=24)  # WhatsApp allows free-form replies within 24 h of the guest


class OutsideServiceWindow(RuntimeError):
    """The guest last wrote more than 24 hours ago: WhatsApp only allows approved templates now."""


@dataclass(frozen=True)
class ConversationSummary:
    id: int
    name: str | None
    phone: str | None
    status: str  # "agent" | "team" | "waiting" | "attention"
    last_text: str
    last_role: str
    last_message_at: datetime


class ConversationService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        sender: MessageSender,
        responder: Responder,
        settings_store: RuntimeSettingsStore,
        retention_hours: int = 48,
        clock: Callable[[], datetime] = utcnow,
    ):
        self._sessions = session_factory
        self._sender = sender
        self._responder = responder
        self._settings = settings_store
        self._retention = timedelta(hours=retention_hours)
        self._now = clock
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._chat_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ── Incoming WhatsApp events ─────────────────────────────────────────────

    async def handle_events(self, events: Sequence[InboundMessage | TeamMessage]) -> list[str]:
        outcomes = []
        for event in events:
            # Webhooks for one guest can arrive at the same time: handle them one by one, in order.
            async with self._chat_locks[event.wa_id]:
                try:
                    if isinstance(event, InboundMessage):
                        outcomes.append(await self.handle_inbound(event))
                    else:
                        outcomes.append(await self.handle_team_message(event))
                except Exception:  # one bad event must not drop the others
                    log.exception("Failed to handle a WhatsApp event")
                    outcomes.append("error")
        return outcomes

    async def handle_inbound(self, event: InboundMessage) -> str:
        now = self._now()
        runtime = await self._settings.load()
        async with self._sessions() as session, session.begin():
            if await self._already_stored(session, event.message_id):
                return "duplicate"
            conversation = await self._get_or_create(session, event.wa_id, event.name)
            session.add(
                Message(
                    conversation_id=conversation.id,
                    role=Role.CUSTOMER,
                    text=event.text,
                    wa_message_id=event.message_id,
                    created_at=event.sent_at,
                )
            )
            conversation.last_customer_at = event.sent_at
            conversation.last_message_at = now
            team_has_chat = conversation.team_active(now)
            if team_has_chat and conversation.waiting_since is None:
                conversation.waiting_since = now
            conversation_id = conversation.id

        if team_has_chat:
            return "team_has_chat"
        if now - event.sent_at > STALE_AFTER:
            return "stale"
        if not runtime.enabled:
            return "agent_paused"
        if not self._sender.configured:
            return "whatsapp_not_configured"
        await self._sender.mark_read(event.message_id)
        return await self.answer(conversation_id)

    async def handle_team_message(self, event: TeamMessage) -> str:
        """Someone from the team wrote from the WhatsApp Business app: the chat is theirs now."""
        runtime = await self._settings.load()
        now = self._now()
        async with self._sessions() as session, session.begin():
            if await self._already_stored(session, event.message_id):
                return "duplicate"  # includes the agent's own replies, stored with their id
            conversation = await self._get_or_create(session, event.wa_id, None)
            session.add(
                Message(
                    conversation_id=conversation.id,
                    role=Role.STAFF,
                    text=event.text,
                    wa_message_id=event.message_id,
                    created_at=event.sent_at,
                )
            )
            self._give_to_team(conversation, now, runtime.takeover_hours)
            # Delivered late: if the guest wrote after this team message and nobody has answered
            # since, the guest is still waiting.
            await session.flush()
            latest = (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(1)
                )
            ).scalar_one()
            if latest.role == Role.CUSTOMER:
                conversation.waiting_since = now
        return "team_took_over"

    # ── Replying ─────────────────────────────────────────────────────────────

    async def answer(self, conversation_id: int, stepping_in: bool = False) -> str:
        async with self._locks[conversation_id]:
            async with self._sessions() as session:
                conversation = await self._load(session, conversation_id)
                if conversation is None:
                    return "not_found"
                if not stepping_in and conversation.team_active(self._now()):
                    return "team_has_chat"
                turns = [Turn(m.role, m.text) for m in conversation.messages[-HISTORY_LIMIT:]]
                guest = GuestContext(conversation.id, conversation.phone, conversation.name)
                wa_id, is_test = conversation.wa_id, conversation.is_test
            if not turns or turns[-1].role != Role.CUSTOMER:
                return "nothing_to_answer"

            try:
                reply = await self._responder.reply(turns, guest, self._now(), stepping_in=stepping_in)
            except AgentUnavailable as exc:
                log.warning("Agent unavailable for conversation %s: %s", conversation_id, exc)
                reply = AgentReply(text=SORRY, handoff_reason=f"The agent could not answer ({exc}).")
            if not reply.text:
                return "empty_reply"

            message_id = None
            if not is_test:
                try:
                    message_id = await self._sender.send_text(wa_id, reply.text)
                except WhatsAppError as exc:
                    log.error("Reply to %s not delivered: %s", mask_phone(f"+{wa_id}"), exc)
                    return "send_failed"

            runtime = await self._settings.load()
            async with self._sessions() as session, session.begin():
                conversation = await session.get(Conversation, conversation_id)
                session.add(
                    Message(
                        conversation_id=conversation_id,
                        role=Role.AGENT,
                        text=reply.text,
                        wa_message_id=message_id,
                        created_at=self._now(),
                    )
                )
                conversation.last_message_at = self._now()
                if reply.handoff_reason:
                    conversation.attention_reason = reply.handoff_reason
                    self._give_to_team(conversation, self._now(), runtime.takeover_hours, keep_attention=True)
            return "sent"

    async def sweep(self) -> int:
        """Answer chats the team took over but left a guest waiting too long. Returns how many."""
        runtime = await self._settings.load()
        if not runtime.enabled:
            return 0
        now = self._now()
        cutoff = now - timedelta(minutes=runtime.reply_after_minutes)
        async with self._sessions() as session:
            ids = (
                (
                    await session.execute(
                        select(Conversation.id).where(
                            Conversation.is_test.is_(False),
                            Conversation.team_until > now,
                            Conversation.waiting_since.is_not(None),
                            Conversation.waiting_since <= cutoff,
                        )
                    )
                )
                .scalars()
                .all()
            )
        answered = 0
        for conversation_id in ids:
            # Claim the chat first, so two overlapping runs never both answer it.
            async with self._sessions() as session, session.begin():
                claimed = await session.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id, Conversation.waiting_since.is_not(None))
                    .values(waiting_since=None)
                )
            if claimed.rowcount != 1:
                continue
            if await self.answer(conversation_id, stepping_in=True) == "sent":
                answered += 1
        if answered:
            log.info("Stepped in on %s waiting conversation(s)", answered)
        return answered

    async def purge(self) -> int:
        """Forget conversations with no message for `retention_hours` (privacy)."""
        cutoff = self._now() - self._retention
        old = select(Conversation.id).where(Conversation.last_message_at < cutoff)
        async with self._sessions() as session, session.begin():
            await session.execute(delete(Message).where(Message.conversation_id.in_(old)))
            result = await session.execute(delete(Conversation).where(Conversation.last_message_at < cutoff))
        return result.rowcount or 0

    # ── The team, from the admin inbox ───────────────────────────────────────

    async def team_reply(self, conversation_id: int, text: str) -> None:
        runtime = await self._settings.load()
        async with self._sessions() as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                raise LookupError("Conversation not found")
            last = as_utc(conversation.last_customer_at)
            if not conversation.is_test and (last is None or self._now() - last > SERVICE_WINDOW):
                raise OutsideServiceWindow
            wa_id, is_test = conversation.wa_id, conversation.is_test
        message_id = None if is_test else await self._sender.send_text(wa_id, text)
        async with self._sessions() as session, session.begin():
            conversation = await session.get(Conversation, conversation_id)
            session.add(
                Message(
                    conversation_id=conversation_id,
                    role=Role.STAFF,
                    text=text,
                    wa_message_id=message_id,
                    created_at=self._now(),
                )
            )
            self._give_to_team(conversation, self._now(), runtime.takeover_hours)

    async def take_over(self, conversation_id: int) -> None:
        runtime = await self._settings.load()
        async with self._sessions() as session, session.begin():
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                raise LookupError("Conversation not found")
            self._give_to_team(conversation, self._now(), runtime.takeover_hours, keep_attention=True)

    async def hand_back(self, conversation_id: int) -> str:
        """Give the chat back to the agent; it answers right away if the guest is waiting."""
        async with self._sessions() as session, session.begin():
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                raise LookupError("Conversation not found")
            conversation.team_until = None
            conversation.waiting_since = None
            conversation.attention_reason = None
        if self._sender.configured:
            return await self.answer(conversation_id)
        return "handed_back"

    # ── Playground (talk to the agent without WhatsApp) ──────────────────────

    async def playground(self, key: str, text: str, phone: str | None, reset: bool = False) -> list[Message]:
        wa_id = f"playground:{key}"
        async with self._sessions() as session, session.begin():
            conversation = await self._get_or_create(session, wa_id, "Playground", is_test=True)
            if reset:
                await session.execute(delete(Message).where(Message.conversation_id == conversation.id))
            conversation.phone = phone
            conversation.team_until = None
            if text.strip():
                session.add(
                    Message(
                        conversation_id=conversation.id, role=Role.CUSTOMER, text=text.strip(), created_at=self._now()
                    )
                )
            conversation.last_customer_at = conversation.last_message_at = self._now()
            conversation_id = conversation.id
        if text.strip():
            await self.answer(conversation_id)
        return await self.messages(conversation_id)

    # ── Reading ──────────────────────────────────────────────────────────────

    async def get(self, conversation_id: int) -> Conversation | None:
        async with self._sessions() as session:
            return await self._load(session, conversation_id)

    async def messages(self, conversation_id: int) -> list[Message]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at, Message.id)
            )
            return list(rows.scalars().all())

    async def summaries(self, limit: int = 100) -> list[ConversationSummary]:
        now = self._now()
        async with self._sessions() as session:
            conversations = (
                (
                    await session.execute(
                        select(Conversation)
                        .where(Conversation.is_test.is_(False))
                        .options(selectinload(Conversation.messages))
                        .order_by(Conversation.last_message_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        out = []
        for c in conversations:
            last = c.messages[-1] if c.messages else None
            out.append(
                ConversationSummary(
                    id=c.id,
                    name=c.name,
                    phone=c.phone,
                    status=self.status_of(c, now),
                    last_text=last.text if last else "",
                    last_role=last.role if last else "",
                    last_message_at=as_utc(c.last_message_at),
                )
            )
        return out

    async def counts(self) -> dict[str, int]:
        summaries = await self.summaries(limit=1000)
        counts = {"total": len(summaries), "agent": 0, "team": 0, "waiting": 0, "attention": 0}
        for summary in summaries:
            counts[summary.status] += 1
        async with self._sessions() as session:
            since = self._now() - timedelta(hours=24)
            counts["agent_replies_24h"] = (
                await session.execute(
                    select(func.count(Message.id)).where(Message.role == Role.AGENT, Message.created_at >= since)
                )
            ).scalar_one()
        return counts

    @staticmethod
    def status_of(conversation: Conversation, now: datetime) -> str:
        if conversation.attention_reason:
            return "attention"
        if conversation.team_active(now):
            return "waiting" if conversation.waiting_since else "team"
        return "agent"

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _give_to_team(conversation: Conversation, now: datetime, hours: int, keep_attention: bool = False) -> None:
        conversation.team_until = now + timedelta(hours=hours)
        conversation.waiting_since = None
        conversation.last_message_at = now
        if not keep_attention:
            conversation.attention_reason = None

    @staticmethod
    async def _already_stored(session: AsyncSession, message_id: str) -> bool:
        found = await session.execute(select(Message.id).where(Message.wa_message_id == message_id))
        return found.first() is not None

    async def _get_or_create(
        self, session: AsyncSession, wa_id: str, name: str | None, is_test: bool = False
    ) -> Conversation:
        conversation = (
            await session.execute(select(Conversation).where(Conversation.wa_id == wa_id))
        ).scalar_one_or_none()
        if conversation is None:
            conversation = await self._insert_or_fetch(session, wa_id, name, is_test)
        if name and conversation.name != name:
            conversation.name = name
        return conversation

    async def _insert_or_fetch(
        self, session: AsyncSession, wa_id: str, name: str | None, is_test: bool
    ) -> Conversation:
        try:
            async with session.begin_nested():
                conversation = Conversation(
                    wa_id=wa_id,
                    phone=None if is_test else from_whatsapp_id(wa_id),
                    name=name,
                    is_test=is_test,
                    last_message_at=self._now(),
                )
                session.add(conversation)
            return conversation
        except IntegrityError:  # created at the same moment by another worker
            return (await session.execute(select(Conversation).where(Conversation.wa_id == wa_id))).scalar_one()

    @staticmethod
    async def _load(session: AsyncSession, conversation_id: int) -> Conversation | None:
        return (
            await session.execute(
                select(Conversation)
                .where(Conversation.id == conversation_id)
                .options(selectinload(Conversation.messages))
            )
        ).scalar_one_or_none()
