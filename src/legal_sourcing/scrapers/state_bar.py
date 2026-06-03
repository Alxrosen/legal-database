"""Generic state-bar scraper.

One scraper subclass, configured at construction by a
``state_bars.StateBarConfig``, instead of a bespoke class per state. It
inherits rate-limiting, retry/backoff, robots handling, and raw-payload
storage from :class:`BaseScraper`; the per-state request shape lives in the
config and the orchestration (sweep + detail fetch) lives in
``pipelines/scrape_state_bar.py``.

Sends a realistic browser header set (many bar sites 403 a bare client) and
flags a Cloudflare/WAF/captcha challenge so a blocked state stops cleanly
rather than ingesting junk.
"""

from __future__ import annotations

import httpx

from legal_sourcing.scrapers.base import BaseScraper, ScrapeError
from legal_sourcing.state_bars import StateBarConfig

# Markers that identify an anti-bot challenge page even on a 2xx response.
_CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "cf-mitigated",
    "attention required",
    "cf_chl_",
    "checking your browser",
    "_incapsula_",
    "incident id",
)


class StateBarChallenge(ScrapeError):
    """Raised when a state-bar site returns an anti-bot challenge. Stops the
    run for that state; we do not try to defeat challenges (same posture as
    Avvo / FindLaw / Justia)."""


class StateBarScraper(BaseScraper):
    ROBOTS_POLICY = "warn"
    WORKERS = 1
    BURST_CAPACITY = 1.0
    MAX_RETRIES = 3
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self, cfg: StateBarConfig) -> None:
        self.cfg = cfg
        # Instance attrs shadow the class attrs BaseScraper.__init__ reads.
        self.SOURCE_NAME = cfg.source
        self.BASE_URL = cfg.base_url
        self.RATE_LIMIT_RPS = cfg.rps
        self.INITIAL_RATE_LIMIT_RPS = cfg.initial_rps
        self.RATE_RAMP_SECONDS = cfg.ramp_seconds
        super().__init__()

    def _default_headers(self) -> dict[str, str]:
        return {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def _check_for_block_response(self, response: httpx.Response) -> None:
        ctype = (response.headers.get("Content-Type") or "").lower()
        if "html" not in ctype:
            return
        body = response.content[:8000].lower()
        for marker in _CHALLENGE_MARKERS:
            if marker.encode("ascii") in body:
                raise StateBarChallenge(
                    f"{self.cfg.source}: anti-bot challenge on {response.url} "
                    f"(marker {marker!r}). Stopping; do not retry. See "
                    f"docs/data_sources/state_bars.md."
                )


__all__ = ["_CHALLENGE_MARKERS", "StateBarChallenge", "StateBarScraper"]
