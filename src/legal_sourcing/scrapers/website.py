"""Firm-website scraper — fetches arbitrary firm sites for content enrichment.

Thin BaseScraper subclass: it inherits rate-limiting, retry, robots handling,
and raw-payload storage, and adds a browser header set + anti-bot detection +
a SHORT timeout / low retry budget so a dead or parked domain fails fast
(recorded `unreachable`) instead of hanging a worker. The producer/worker
pipeline (pipelines/enrich_websites.py) drives the per-firm crawl; this class
just fetches one URL at a time politely.

Each worker crawls a DIFFERENT host sequentially (home -> about/team/attorneys),
so the shared global rate limiter + the one-firm-at-a-time-per-worker pattern
gives effective per-host politeness without a per-host limiter.
"""

from __future__ import annotations

import httpx

from legal_sourcing.scrapers.base import BaseScraper, ScrapeError

_CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "cf-mitigated",
    "attention required",
    "cf_chl_",
    "checking your browser",
    "enable javascript and cookies",
    "_incapsula_",
)


class WebsiteBlocked(ScrapeError):
    """Firm site returned an anti-bot challenge. Recorded + skipped (we do not
    fight challenges); a future headless pass could revisit."""


class FirmWebsiteScraper(BaseScraper):
    SOURCE_NAME = "firm_websites"
    # Placeholder; robots + storage use the per-URL host, not this.
    BASE_URL = "https://firm-websites.invalid"

    # Many distinct hosts, one request at a time per worker -> a modest global
    # cap keeps total load polite. Workers come from the pipeline's pool.
    RATE_LIMIT_RPS = 8.0
    BURST_CAPACITY = 2.0
    WORKERS = 1

    # Fail fast on dead/slow domains so a worker never hangs the crawl.
    TIMEOUT_SECONDS = 15.0
    MAX_RETRIES = 1
    BACKOFF_MAX_SECONDS = 8.0

    ROBOTS_POLICY = "warn"

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def _default_headers(self) -> dict[str, str]:
        return {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        }

    def _check_for_block_response(self, response: httpx.Response) -> None:
        ctype = (response.headers.get("Content-Type") or "").lower()
        if "html" not in ctype:
            return
        body = response.content[:6000].lower()
        title = body.split(b"<title>", 1)[-1][:80] if b"<title>" in body else b""
        for marker in _CHALLENGE_MARKERS:
            mb = marker.encode("ascii")
            if mb in title or (mb in body and marker in ("cf_chl_", "_incapsula_")):
                raise WebsiteBlocked(f"anti-bot challenge on {response.url} (marker {marker!r})")


__all__ = ["_CHALLENGE_MARKERS", "FirmWebsiteScraper", "WebsiteBlocked"]
