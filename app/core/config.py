"""Application settings, read from the environment (and a local `.env` file in development)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Application ───────────────────────────────────────────────────────────
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./gira_agent.db"
    auto_create_schema: bool = Field(
        default=False, description="Create tables at startup (local development and tests; production uses Alembic)."
    )
    business_profile_path: Path = Path("config/business.yaml")

    # ── Restaurant booking API ────────────────────────────────────────────────
    booking_api_url: str = "https://gira-lyart.vercel.app"
    booking_api_timeout_seconds: float = 20.0

    # ── WhatsApp Cloud API (Meta) ─────────────────────────────────────────────
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_verify_token: SecretStr = SecretStr("")
    whatsapp_api_version: str = "v23.0"
    whatsapp_graph_url: str = "https://graph.facebook.com"

    # ── Claude ────────────────────────────────────────────────────────────────
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Effort = "low"
    anthropic_max_tokens: int = 16000
    anthropic_refusal_fallback: bool = True

    # ── Admin interface ───────────────────────────────────────────────────────
    admin_username: str = "admin"
    admin_password: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("dev-only-change-me")
    session_https_only: bool = False

    # ── Background work ───────────────────────────────────────────────────────
    run_scheduler: bool = True
    sweep_interval_seconds: int = 60
    retention_hours: int = 48
    internal_token: SecretStr = SecretStr("")

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.whatsapp_access_token.get_secret_value() and self.whatsapp_phone_number_id)

    @property
    def claude_configured(self) -> bool:
        return bool(self.anthropic_api_key.get_secret_value())

    def production_problems(self) -> list[str]:
        """Settings that must be set before the service can run in production."""
        if self.app_env != "production":
            return []
        required = {
            "WHATSAPP_ACCESS_TOKEN": self.whatsapp_access_token.get_secret_value(),
            "WHATSAPP_PHONE_NUMBER_ID": self.whatsapp_phone_number_id,
            "WHATSAPP_APP_SECRET": self.whatsapp_app_secret.get_secret_value(),
            "WHATSAPP_VERIFY_TOKEN": self.whatsapp_verify_token.get_secret_value(),
            "ANTHROPIC_API_KEY": self.anthropic_api_key.get_secret_value(),
            "ADMIN_PASSWORD": self.admin_password.get_secret_value(),
        }
        problems = [f"{name} is not set" for name, value in required.items() if not value]
        if self.session_secret.get_secret_value() == "dev-only-change-me":
            problems.append("SESSION_SECRET must be changed")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
