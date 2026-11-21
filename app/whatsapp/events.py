"""Turns WhatsApp Cloud API webhook payloads into the two events the agent cares about.

* InboundMessage — a guest wrote to the restaurant.
* TeamMessage    — someone from the team replied from the WhatsApp Business app on the same
                   number (coexistence "smb_message_echoes"); the chat is then theirs.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

UNREADABLE = "[The guest sent a {kind} you can't open. Kindly ask them to write their message instead.]"


@dataclass(frozen=True)
class InboundMessage:
    wa_id: str
    name: str | None
    message_id: str
    text: str
    sent_at: datetime


@dataclass(frozen=True)
class TeamMessage:
    wa_id: str  # the guest the team wrote to
    message_id: str
    text: str
    sent_at: datetime


def _timestamp(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _text_of(message: dict[str, Any]) -> str | None:
    kind = message.get("type")
    if kind == "text":
        return (message.get("text") or {}).get("body")
    if kind == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return reply.get("title")
    if kind == "button":
        return (message.get("button") or {}).get("text")
    if kind in {"image", "video", "document"}:
        caption = (message.get(kind) or {}).get("caption")
        return caption or UNREADABLE.format(kind=kind)
    if kind in {"audio", "voice"}:
        return UNREADABLE.format(kind="voice note")
    if kind == "location":
        return "[The guest shared a location.]"
    if kind in {"sticker", "reaction", "unsupported", "system", "ephemeral"}:
        return None
    return None


def parse_webhook(payload: dict[str, Any], phone_number_id: str) -> list[InboundMessage | TeamMessage]:
    """Events for our number, oldest first. Delivery statuses and other numbers are ignored."""
    events: list[InboundMessage | TeamMessage] = []
    if payload.get("object") != "whatsapp_business_account":
        return events
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            if phone_number_id and metadata.get("phone_number_id") not in (None, phone_number_id):
                continue
            field = change.get("field")
            if field == "messages":
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
                for message in value.get("messages") or []:
                    text = _text_of(message)
                    wa_id = message.get("from")
                    if not text or not wa_id or not message.get("id"):
                        continue
                    events.append(
                        InboundMessage(
                            wa_id=wa_id,
                            name=names.get(wa_id),
                            message_id=message["id"],
                            text=text.strip(),
                            sent_at=_timestamp(message.get("timestamp")),
                        )
                    )
            elif field == "smb_message_echoes":
                for echo in value.get("message_echoes") or []:
                    text = _text_of(echo) or "[The team sent a message.]"
                    if not echo.get("to") or not echo.get("id"):
                        continue
                    events.append(
                        TeamMessage(
                            wa_id=echo["to"],
                            message_id=echo["id"],
                            text=text.strip(),
                            sent_at=_timestamp(echo.get("timestamp")),
                        )
                    )
    events.sort(key=lambda event: event.sent_at)
    return events
