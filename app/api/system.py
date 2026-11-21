"""Health checks and the endpoint an external scheduler can call."""

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import text

from app import __version__
from app.api.deps import get_container
from app.container import Container

router = APIRouter(tags=["system"])


@router.get("/health", summary="Liveness and readiness")
async def health(container: Container = Depends(get_container)) -> dict[str, object]:
    try:
        async with container.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        database = "unavailable"
    settings = container.settings
    return {
        "status": "ok" if database == "ok" else "degraded",
        "version": __version__,
        "database": database,
        "whatsapp": "configured" if settings.whatsapp_configured else "not configured",
        "claude": "configured" if settings.claude_configured else "not configured",
    }


@router.post("/internal/sweep", summary="Run the safety net now (for an external cron)")
async def sweep(
    x_internal_token: str = Header(default=""), container: Container = Depends(get_container)
) -> dict[str, int]:
    expected = container.settings.internal_token.get_secret_value()
    if not expected or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(status_code=403, detail="Forbidden")
    answered = await container.conversations.sweep()
    removed = await container.conversations.purge()
    return {"answered": answered, "removed": removed}
