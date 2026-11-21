import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.whatsapp.client import WhatsAppClient, WhatsAppError, split_text
from app.whatsapp.events import InboundMessage, parse_webhook

GRAPH = "https://graph.facebook.com/v23.0/PNID-1/messages"


@respx.mock
async def test_send_text(settings):
    route = respx.post(GRAPH).respond(json={"messages": [{"id": "wamid.sent"}]})
    async with httpx.AsyncClient() as http:
        message_id = await WhatsAppClient(settings, http).send_text("+21620123456", "Ciao!")
    assert message_id == "wamid.sent"
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer token"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "21620123456",
        "type": "text",
        "text": {"preview_url": True, "body": "Ciao!"},
    }


@respx.mock
async def test_send_errors_are_raised(settings):
    respx.post(GRAPH).respond(400, json={"error": {"code": 131047, "message": "Re-engagement message"}})
    async with httpx.AsyncClient() as http:
        with pytest.raises(WhatsAppError, match="131047"):
            await WhatsAppClient(settings, http).send_text("21620123456", "Hello")


@respx.mock
async def test_mark_read_never_raises(settings):
    respx.post(GRAPH).respond(500)
    async with httpx.AsyncClient() as http:
        await WhatsAppClient(settings, http).mark_read("wamid.in")


def test_long_replies_are_split():
    parts = split_text("word " * 2000, limit=4096)
    assert len(parts) == 3
    assert all(len(part) <= 4096 for part in parts)


def _payload(message: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "PNID-1"},
                            "contacts": [{"wa_id": "212612345678", "profile": {"name": "Yassine"}}],
                            "messages": [
                                {"from": "212612345678", "id": "wamid.1", "timestamp": "1763722800", **message}
                            ],
                        },
                    }
                ]
            }
        ],
    }


@pytest.mark.parametrize(
    ("message", "text"),
    [
        ({"type": "text", "text": {"body": " Salam, wach kayn blassa? "}}, "Salam, wach kayn blassa?"),
        ({"type": "interactive", "interactive": {"button_reply": {"id": "y", "title": "Oui"}}}, "Oui"),
        ({"type": "button", "button": {"text": "Book"}}, "Book"),
        ({"type": "image", "image": {"caption": "This dish?"}}, "This dish?"),
    ],
)
def test_parse_guest_messages(message, text):
    [event] = parse_webhook(_payload(message), "PNID-1")
    assert isinstance(event, InboundMessage)
    assert event.text == text
    assert event.name == "Yassine"
    assert event.sent_at == datetime.fromtimestamp(1763722800, UTC)


def test_voice_notes_ask_the_guest_to_write():
    [event] = parse_webhook(_payload({"type": "audio", "audio": {"id": "m"}}), "PNID-1")
    assert "voice note" in event.text


def test_stickers_and_reactions_are_ignored():
    assert parse_webhook(_payload({"type": "reaction", "reaction": {"emoji": "👍"}}), "PNID-1") == []
    assert parse_webhook({"object": "page"}, "PNID-1") == []
