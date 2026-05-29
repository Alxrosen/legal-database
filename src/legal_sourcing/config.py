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

    # HTTP / scraping — these are GLOBAL FALLBACKS. Each scraper is
    # expected to override `rate_limit_rps` and `max_workers` based on the
    # target server's capacity. See docs/assumptions.md.
    user_agent: str = Field(default="legal-sourcing-research/0.1")
    rate_limit_rps: float = Field(default=8.0, ge=0.0)
    max_workers: int = Field(default=6, ge=1)
    request_timeout_seconds: float = Field(default=30.0, gt=0.0)

    # Logging
    log_level: str = Field(default="INFO")

    # Per-source secrets. Empty by default — the corresponding scraper
    # fails loud at startup with a useful error message if its secret
    # is required but unset. See docs/data_sources/az_bar_reference.md
    # for how to obtain the AZ Bar Password header (DevTools capture).
    azbar_api_password: str = Field(default="")

    @property
    def db_url(self) -> str:
        """SQLAlchemy URL. Swap-in point when we move off SQLite."""
        return f"sqlite:///{self.db_path.resolve()}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
