"""Wires the application's parts together once, at startup."""

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent.runner import ClaudeAgent, Responder
from app.agent.tools import Toolbox
from app.booking.client import BookingClient
from app.business.profile import BusinessProfile, load_profile
from app.core.config import Settings
from app.db.session import create_engine, create_session_factory
from app.services.conversations import ConversationService
from app.services.runtime_settings import RuntimeSettingsStore
from app.services.scheduler import Scheduler
from app.whatsapp.client import MessageSender, WhatsAppClient


@dataclass
class Container:
    settings: Settings
    profile: BusinessProfile
    engine: AsyncEngine
    sessions: async_sessionmaker
    http: httpx.AsyncClient
    sender: MessageSender
    runtime_settings: RuntimeSettingsStore
    conversations: ConversationService
    scheduler: Scheduler

    async def close(self) -> None:
        await self.scheduler.stop()
        await self.http.aclose()
        await self.engine.dispose()


def build_container(
    settings: Settings,
    sender: MessageSender | None = None,
    responder: Responder | None = None,
    profile: BusinessProfile | None = None,
) -> Container:
    profile = profile or load_profile(settings.business_profile_path)
    engine = create_engine(settings.database_url)
    sessions = create_session_factory(engine)
    http = httpx.AsyncClient()
    sender = sender or WhatsAppClient(settings, http)
    booking = BookingClient(settings.booking_api_url, http, settings.booking_api_timeout_seconds)
    responder = responder or ClaudeAgent(settings, profile, Toolbox(profile, booking))
    runtime_settings = RuntimeSettingsStore(sessions)
    conversations = ConversationService(
        sessions, sender, responder, runtime_settings, retention_hours=settings.retention_hours
    )
    return Container(
        settings=settings,
        profile=profile,
        engine=engine,
        sessions=sessions,
        http=http,
        sender=sender,
        runtime_settings=runtime_settings,
        conversations=conversations,
        scheduler=Scheduler(conversations, settings.sweep_interval_seconds),
    )
