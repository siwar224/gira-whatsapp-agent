"""Behaviour the team can change from the admin interface, stored in the database."""

from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Setting


@dataclass
class RuntimeSettings:
    enabled: bool = True
    # In a chat the team took over: minutes without a team reply before the agent steps in.
    reply_after_minutes: int = 30
    # How long a chat stays with the team after their last message.
    takeover_hours: int = 12


class RuntimeSettingsStore:
    def __init__(self, session_factory: async_sessionmaker):
        self._sessions = session_factory

    async def load(self) -> RuntimeSettings:
        async with self._sessions() as session:
            rows = (await session.execute(select(Setting))).scalars().all()
        stored = {row.key: row.value for row in rows}
        defaults = RuntimeSettings()
        return RuntimeSettings(
            enabled=stored.get("enabled", str(defaults.enabled)) == "True",
            reply_after_minutes=int(stored.get("reply_after_minutes", defaults.reply_after_minutes)),
            takeover_hours=int(stored.get("takeover_hours", defaults.takeover_hours)),
        )

    async def save(self, settings: RuntimeSettings) -> None:
        async with self._sessions() as session, session.begin():
            for key, value in asdict(settings).items():
                await session.merge(Setting(key=key, value=str(value)))
