"""The team's web interface: dashboard, inbox, conversations, settings and a playground."""

import secrets
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import __version__
from app.admin.auth import LoginThrottle, check_credentials, check_csrf, csrf_token, require_admin
from app.api.deps import get_container
from app.business.phone import country_of, normalize_phone
from app.container import Container
from app.db.models import as_utc, utcnow
from app.services.conversations import SERVICE_WINDOW, ConversationService, OutsideServiceWindow
from app.services.runtime_settings import RuntimeSettings
from app.whatsapp.client import WhatsAppError

router = APIRouter(prefix="/admin", include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
throttle = LoginThrottle()

STATUS_LABELS = {
    "agent": "Agent",
    "team": "Team",
    "waiting": "Guest waiting",
    "attention": "Needs a person",
}


def _flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def _render(request: Request, template: str, container: Container, **context) -> HTMLResponse:
    profile = container.profile

    def local_time(value: datetime | None, fmt: str = "%d %b %H:%M") -> str:
        value = as_utc(value)
        return value.astimezone(profile.tz).strftime(fmt) if value else ""

    def flag(phone: str | None) -> str:
        country = country_of(phone)
        return country.flag if country else ""

    templates.env.filters["local_time"] = local_time
    templates.env.filters["flag"] = flag
    return templates.TemplateResponse(
        request,
        template,
        {
            "csrf": csrf_token(request),
            "flash": request.session.pop("flash", None),
            "user": request.session.get("admin"),
            "restaurant": profile.name,
            "version": __version__,
            "status_labels": STATUS_LABELS,
            **context,
        },
    )


# ── Sign in ────────────────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, container: Container = Depends(get_container)):
    configured = bool(container.settings.admin_password.get_secret_value())
    return _render(request, "login.html", container, configured=configured)


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    csrf: str = Form(""),
    container: Container = Depends(get_container),
):
    check_csrf(request, csrf)
    key = request.client.host if request.client else "unknown"
    if throttle.blocked(key):
        _flash(request, "Too many attempts. Try again in 15 minutes.", "error")
        return RedirectResponse("/admin/login", status_code=303)
    if not check_credentials(container.settings, username, password):
        throttle.failed(key)
        _flash(request, "Wrong username or password.", "error")
        return RedirectResponse("/admin/login", status_code=303)
    throttle.reset(key)
    request.session.clear()
    request.session["admin"] = username
    request.session["csrf"] = secrets.token_urlsafe(32)
    return RedirectResponse("/admin", status_code=303)


@router.post("/logout")
async def logout(request: Request, csrf: str = Form("")):
    check_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


# ── Dashboard and settings ─────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, _: str = Depends(require_admin), container: Container = Depends(get_container)):
    return _render(
        request,
        "dashboard.html",
        container,
        counts=await container.conversations.counts(),
        runtime=await container.runtime_settings.load(),
        settings=container.settings,
        profile=container.profile,
        recent=(await container.conversations.summaries(limit=6)),
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    csrf: str = Form(""),
    enabled: bool = Form(False),
    reply_after_minutes: int = Form(30, ge=1, le=1440),
    takeover_hours: int = Form(12, ge=1, le=168),
    _: str = Depends(require_admin),
    container: Container = Depends(get_container),
):
    check_csrf(request, csrf)
    await container.runtime_settings.save(
        RuntimeSettings(enabled=enabled, reply_after_minutes=reply_after_minutes, takeover_hours=takeover_hours)
    )
    _flash(request, "Settings saved.")
    return RedirectResponse("/admin", status_code=303)


# ── Inbox and conversations ────────────────────────────────────────────────


@router.get("/conversations", response_class=HTMLResponse)
async def inbox(
    request: Request,
    status: str = "",
    _: str = Depends(require_admin),
    container: Container = Depends(get_container),
):
    summaries = await container.conversations.summaries()
    if status in STATUS_LABELS:
        summaries = [s for s in summaries if s.status == status]
    return _render(request, "inbox.html", container, conversations=summaries, current=status)


@router.get("/conversations/{conversation_id}", response_class=HTMLResponse)
async def conversation_page(
    conversation_id: int,
    request: Request,
    _: str = Depends(require_admin),
    container: Container = Depends(get_container),
):
    conversation = await container.conversations.get(conversation_id)
    if conversation is None or conversation.is_test:
        raise HTTPException(status_code=404)
    now = utcnow()
    last_customer = as_utc(conversation.last_customer_at)
    return _render(
        request,
        "conversation.html",
        container,
        conversation=conversation,
        status=ConversationService.status_of(conversation, now),
        window_open=bool(last_customer and now - last_customer <= SERVICE_WINDOW),
        team_until=as_utc(conversation.team_until),
    )


@router.post("/conversations/{conversation_id}/reply")
async def reply(
    conversation_id: int,
    request: Request,
    text: str = Form(..., min_length=1, max_length=4000),
    csrf: str = Form(""),
    _: str = Depends(require_admin),
    container: Container = Depends(get_container),
):
    check_csrf(request, csrf)
    try:
        await container.conversations.team_reply(conversation_id, text.strip())
        _flash(request, "Sent. The chat is yours — the agent stays out of it.")
    except OutsideServiceWindow:
        _flash(
            request,
            "The guest last wrote more than 24 hours ago; WhatsApp only allows a reply after they write again.",
            "error",
        )
    except WhatsAppError as exc:
        _flash(request, f"WhatsApp refused the message: {exc}", "error")
    except LookupError as exc:
        raise HTTPException(status_code=404) from exc
    return RedirectResponse(f"/admin/conversations/{conversation_id}", status_code=303)


@router.post("/conversations/{conversation_id}/take-over")
async def take_over(
    conversation_id: int,
    request: Request,
    csrf: str = Form(""),
    _: str = Depends(require_admin),
    container: Container = Depends(get_container),
):
    check_csrf(request, csrf)
    await container.conversations.take_over(conversation_id)
    _flash(request, "You have this chat. The agent only steps in if a guest waits too long.")
    return RedirectResponse(f"/admin/conversations/{conversation_id}", status_code=303)


@router.post("/conversations/{conversation_id}/hand-back")
async def hand_back(
    conversation_id: int,
    request: Request,
    csrf: str = Form(""),
    _: str = Depends(require_admin),
    container: Container = Depends(get_container),
):
    check_csrf(request, csrf)
    outcome = await container.conversations.hand_back(conversation_id)
    message = "The agent has the chat again."
    if outcome == "sent":
        message += " It just answered the guest's last message."
    _flash(request, message)
    return RedirectResponse(f"/admin/conversations/{conversation_id}", status_code=303)


# ── Playground ─────────────────────────────────────────────────────────────


@router.get("/playground", response_class=HTMLResponse)
async def playground_page(
    request: Request, _: str = Depends(require_admin), container: Container = Depends(get_container)
):
    key = request.session.setdefault("playground", secrets.token_hex(8))
    messages = await container.conversations.playground(key, "", request.session.get("playground_phone"))
    return _render(
        request, "playground.html", container, messages=messages, phone=request.session.get("playground_phone", "")
    )


@router.post("/playground")
async def playground_send(
    request: Request,
    text: str = Form("", max_length=2000),
    phone: str = Form(""),
    reset: bool = Form(False),
    csrf: str = Form(""),
    _: str = Depends(require_admin),
    container: Container = Depends(get_container),
):
    check_csrf(request, csrf)
    key = request.session.setdefault("playground", secrets.token_hex(8))
    normalized = normalize_phone(phone, container.profile.country)
    if phone and not normalized:
        _flash(request, "That phone number doesn't look valid.", "error")
        return RedirectResponse("/admin/playground", status_code=303)
    request.session["playground_phone"] = normalized or ""
    await container.conversations.playground(key, "" if reset else text, normalized, reset=reset)
    return RedirectResponse("/admin/playground", status_code=303)
