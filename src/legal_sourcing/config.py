"""Project-wide configuration loaded from environment / .env file.

Use `get_settings()` everywhere instead of reading env vars directly. The
function is cached so a process sees a single, consistent Settings instance.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Storage
    db_path: Path = Field(default=Path("./data/legal_sourcing.sqlite"))
    raw_data_dir: Path = Field(default=Path("./data/raw"))
    processed_data_dir: Path = Field(default=Path("./data/processed"))

    # HTTP / scraping
    user_agent: str = Field(
        default="legal-sourcing-research/0.1 (+contact: you@example.com)"
    )
    rate_limit_rps: float = Field(default=1.0, ge=0.0)
    request_timeout_seconds: float = Field(default=30.0, gt=0.0)

    # Logging
    log_level: str = Field(default="INFO")

    @property
    def db_url(self) -> str:
        """SQLAlchemy URL. Swap-in point when we move off SQLite."""
        return f"sqlite:///{self.db_path.resolve()}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
