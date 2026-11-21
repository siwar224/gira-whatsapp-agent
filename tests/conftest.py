import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.agent.runner import AgentReply
from app.business.profile import load_profile
from app.core.config import Settings
from app.db.session import create_schema
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
APP_SECRET = "app-secret"
PHONE_NUMBER_ID = "PNID-1"


class FakeSender:
    configured = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.read: list[str] = []

    async def send_text(self, to: str, body: str) -> str | None:
        self.sent.append((to, body))
        return f"wamid.out-{len(self.sent)}"

    async def mark_read(self, message_id: str, typing: bool = True) -> None:
        self.read.append(message_id)


class FakeResponder:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.text = "Ciao! How can I help?"
        self.handoff: str | None = None

    async def reply(self, turns, guest, now, stepping_in=False):
        self.calls.append({"turns": turns, "guest": guest, "stepping_in": stepping_in})
        return AgentReply(text=self.text, handoff_reason=self.handoff)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 21, 11, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def profile():
    return load_profile(ROOT / "config" / "business.yaml")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        business_profile_path=ROOT / "config" / "business.yaml",
        run_scheduler=False,
        whatsapp_access_token="token",
        whatsapp_phone_number_id=PHONE_NUMBER_ID,
        whatsapp_app_secret=APP_SECRET,
        whatsapp_verify_token="verify-me",
        anthropic_api_key="test-key",
        admin_password="correct horse battery",
        internal_token="internal-token",
    )


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def responder() -> FakeResponder:
    return FakeResponder()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def app(settings, sender, responder, clock):
    application = create_app(settings, sender=sender, responder=responder)
    container = application.state.container
    container.conversations._now = clock
    await create_schema(container.engine)
    yield application
    await container.close()


@pytest.fixture
def container(app):
    return app.state.container


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


# ── Webhook payloads ─────────────────────────────────────────────────────────


def inbound_payload(
    wa_id: str, text: str, message_id: str, at: datetime, name: str = "Lea", phone_number_id: str = PHONE_NUMBER_ID
) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "33100000000", "phone_number_id": phone_number_id},
                            "contacts": [{"profile": {"name": name}, "wa_id": wa_id}],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": message_id,
                                    "timestamp": str(int(at.timestamp())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def echo_payload(to: str, text: str, message_id: str, at: datetime) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "smb_message_echoes",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "33100000000", "phone_number_id": PHONE_NUMBER_ID},
                            "message_echoes": [
                                {
                                    "from": "33100000000",
                                    "to": to,
                                    "id": message_id,
                                    "timestamp": str(int(at.timestamp())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def post_webhook(client: httpx.AsyncClient, payload: dict, secret: str = APP_SECRET) -> httpx.Response:
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return await client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no CSRF token in page"
    return match.group(1)


async def sign_in(client: httpx.AsyncClient, password: str = "correct horse battery") -> None:
    page = await client.get("/admin/login")
    response = await client.post(
        "/admin/login",
        data={"username": "admin", "password": password, "csrf": csrf_from(page.text)},
    )
    assert response.status_code == 303
