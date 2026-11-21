"""The WhatsApp Cloud API webhook: verification handshake and incoming events."""

import json
import logging
import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.api.deps import get_container
from app.container import Container
from app.whatsapp.events import parse_webhook
from app.whatsapp.signature import is_valid_signature

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["whatsapp"])


@router.get("", response_class=PlainTextResponse, summary="Webhook verification (Meta)")
async def verify(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
    container: Container = Depends(get_container),
) -> str:
    expected = container.settings.whatsapp_verify_token.get_secret_value()
    if mode == "subscribe" and expected and secrets.compare_digest(token, expected):
        return challenge
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("", summary="Incoming WhatsApp events")
async def receive(
    request: Request, background: BackgroundTasks, container: Container = Depends(get_container)
) -> dict[str, str]:
    body = await request.body()
    app_secret = container.settings.whatsapp_app_secret.get_secret_value()
    must_verify = bool(app_secret) or container.settings.app_env == "production"
    if must_verify and not is_valid_signature(body, request.headers.get("X-Hub-Signature-256"), app_secret):
        raise HTTPException(status_code=403, detail="Invalid signature")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Body is not JSON") from exc

    events = parse_webhook(payload, container.settings.whatsapp_phone_number_id)
    if events:
        # Acknowledge at once (Meta retries slow webhooks); replies are written in the background.
        background.add_task(container.conversations.handle_events, events)
    return {"status": "received"}
