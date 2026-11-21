"""FastAPI application: WhatsApp webhook, admin interface, health checks."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.admin.auth import LoginRequired
from app.admin.routes import router as admin_router
from app.agent.runner import Responder
from app.api.system import router as system_router
from app.api.webhook import router as webhook_router
from app.container import build_container
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_schema
from app.whatsapp.client import MessageSender

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    sender: MessageSender | None = None,
    responder: Responder | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    problems = settings.production_problems()
    if problems:
        raise RuntimeError("Missing production settings: " + "; ".join(problems))

    container = build_container(settings, sender=sender, responder=responder)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.auto_create_schema:
            await create_schema(container.engine)
        if settings.run_scheduler:
            container.scheduler.start()
        log.info("GIRA WhatsApp host %s ready (%s)", __version__, settings.app_env)
        yield
        await container.close()

    app = FastAPI(
        title="GIRA WhatsApp Agent",
        description="A WhatsApp host that answers guests and manages their bookings.",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url=None,
    )
    app.state.container = container
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        session_cookie="gira_admin",
        max_age=12 * 3600,
        same_site="lax",
        https_only=settings.session_https_only,
    )
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "admin" / "static"), name="static")
    app.include_router(webhook_router)
    app.include_router(system_router)
    app.include_router(admin_router)

    @app.exception_handler(LoginRequired)
    async def to_login(request: Request, exc: LoginRequired) -> RedirectResponse:
        return RedirectResponse("/admin/login", status_code=303)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin", status_code=303)

    return app
