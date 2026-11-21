"""Background loop: the 30-minute safety net and the privacy clean-up."""

import asyncio
import contextlib
import logging
from datetime import timedelta

from app.db.models import utcnow
from app.services.conversations import ConversationService

log = logging.getLogger(__name__)

PURGE_EVERY = timedelta(hours=1)


class Scheduler:
    def __init__(self, conversations: ConversationService, interval_seconds: int):
        self._conversations = conversations
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._last_purge = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="gira-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def tick(self) -> None:
        await self._conversations.sweep()
        now = utcnow()
        if self._last_purge is None or now - self._last_purge >= PURGE_EVERY:
            removed = await self._conversations.purge()
            self._last_purge = now
            if removed:
                log.info("Removed %s old conversation(s)", removed)

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # keep the loop alive whatever happens
                log.exception("Scheduled run failed")
            await asyncio.sleep(self._interval)
