"""Sending messages with the WhatsApp Cloud API."""

import logging
from typing import Protocol

import httpx

from app.core.config import Settings
from app.core.logging import mask_phone

log = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 4096


class WhatsAppError(RuntimeError):
    """WhatsApp refused a request (expired token, closed 24-hour window, unknown number…)."""


class MessageSender(Protocol):
    @property
    def configured(self) -> bool: ...

    async def send_text(self, to: str, body: str) -> str | None: ...

    async def mark_read(self, message_id: str, typing: bool = True) -> None: ...


def split_text(body: str, limit: int = MAX_TEXT_LENGTH) -> list[str]:
    """WhatsApp caps a text at 4096 characters; long replies go out in several parts."""
    parts: list[str] = []
    rest = body.strip()
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


class WhatsAppClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self._settings = settings
        self._http = http

    @property
    def configured(self) -> bool:
        return self._settings.whatsapp_configured

    @property
    def _url(self) -> str:
        s = self._settings
        return f"{s.whatsapp_graph_url.rstrip('/')}/{s.whatsapp_api_version}/{s.whatsapp_phone_number_id}/messages"

    async def _post(self, payload: dict) -> dict:
        token = self._settings.whatsapp_access_token.get_secret_value()
        response = await self._http.post(
            self._url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=20
        )
        if response.status_code >= 400:
            try:
                error = response.json().get("error", {})
            except ValueError:
                error = {}
            raise WhatsAppError(
                f"WhatsApp API {response.status_code}: {error.get('code', '')} {error.get('message', '')}".strip()
            )
        return response.json()

    async def send_text(self, to: str, body: str) -> str | None:
        """Send a text to a guest (WhatsApp id or E.164 number). Returns the last message id."""
        recipient = to.lstrip("+")
        message_id = None
        for part in split_text(body):
            data = await self._post(
                {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": recipient,
                    "type": "text",
                    "text": {"preview_url": True, "body": part},
                }
            )
            message_id = ((data.get("messages") or [{}])[0]).get("id") or message_id
        log.info("Sent WhatsApp message to %s", mask_phone(f"+{recipient}"))
        return message_id

    async def mark_read(self, message_id: str, typing: bool = True) -> None:
        """Blue ticks on the guest's message and a 'typing…' bubble while the reply is written."""
        payload: dict = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
        if typing:
            payload["typing_indicator"] = {"type": "text"}
        try:
            await self._post(payload)
        except (WhatsAppError, httpx.HTTPError) as exc:  # cosmetic: never block a reply on it
            log.warning("Could not mark message as read: %s", exc)
