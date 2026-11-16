"""Application settings loaded from the environment."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every value can be overridden via ``RETURNPROOF_*`` env vars."""

    model_config = SettingsConfigDict(
        env_prefix="RETURNPROOF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "ReturnProof"
    environment: str = "development"
    debug: bool = False
    secret_key: str = "dev-only-change-me"
    database_url: str = "sqlite:///./returnproof.db"
    base_url: str = "http://localhost:8000"
    upload_dir: Path = Path("var/evidence")
    storage_backend: str = "local"
    max_upload_mb: int = 50
    session_cookie: str = "returnproof_session"
    session_max_age_seconds: int = 60 * 60 * 12
    timezone: str = "Asia/Kolkata"
    currency: str = "INR"
    # Product-identity dHash distance below/equal this counts as a visual match.
    phash_threshold: int = 8
    weight_tolerance_grams: int = 25
    weight_tolerance_percent: float = 5.0
    claim_window_default_days: int = 15
    rate_limit_auth_per_minute: int = 10
    demo_mode: bool = Field(default=False, description="Show demo banner and sample-data hints.")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
