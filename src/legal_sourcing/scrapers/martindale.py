"""Martindale-Hubbell directory scraper.

Targets the HTML site at https://www.martindale.com per
docs/data_sources/martindale.md. Unlike AZ Bar (JSON API), this source
is server-rendered HTML — the JSON-LD blocks inside the pages are the
parser's primary signal.

Behavior decisions captured from the data-source doc:

* **Cloudflare-fronted.** If we get 403 / 503 / interstitial, **stop**
  the run and do not escalate tooling. The base scraper already
  surfaces non-retryable 4xx as ScrapeError with the body excerpt.
* **Polite default rate.** 0.5 RPS, burst 2, ramp from 0.25 -> 0.5
  over 60s. The doc proposes 0.5 RPS as the pilot target; we run a
  short ramp into it so the first burst doesn't trip a WAF.
* **robots.txt.** Project default `"warn"` policy applies. The doc
  enumerates the only deny paths under `User-agent: *` and our
  traversal stays outside them; the warn-log will catch any
  accidental misses.
* **Forbidden-path deny list.** Enforced at URL-queue time by
  `is_path_allowed()` below — checked before robots, so even if the
  warn-mode policy lets a URL through, our own deny list short-
  circuits it.

This module is the scaffold — the recon script
(`scripts/recon_martindale.py`) and the eventual pipeline will use
`fetch_one()` from the base scraper directly with appropriate
`bucket` and `filename` arguments.
"""

from __future__ import annotations

from urllib.parse import urlparse

from legal_sourcing.scrapers.base import BaseScraper


# Paths the site's robots.txt forbids under `User-agent: *`. Our own
# pre-check keeps us honest in case the warn-mode policy lets one slip.
_FORBIDDEN_PATH_PREFIXES = (
    "/legal-news/",
    "/marketyourfirm/",
    "/document-type/white-papers/articles/",
    "/cdn-cgi/",
    "/assets/html/profiles/",
)


def is_path_allowed(url: str) -> bool:
    """Return False if the URL falls under any robots-forbidden prefix."""
    path = urlparse(url).path or "/"
    for bad in _FORBIDDEN_PATH_PREFIXES:
        if path.startswith(bad):
            return False
    return True


class MartindaleScraper(BaseScraper):
    SOURCE_NAME = "martindale"
    BASE_URL = "https://www.martindale.com"

    # Doc-recommended pilot rate. The Cloudflare layer in front of
    # Martindale is sensitive; start slow, ramp briefly.
    RATE_LIMIT_RPS = 0.5
    INITIAL_RATE_LIMIT_RPS = 0.25
    RATE_RAMP_SECONDS = 60.0
    BURST_CAPACITY = 2.0
    WORKERS = 2

    ROBOTS_POLICY = "warn"  # project default; deny list below is the real gate
    MAX_RETRIES = 5
    BACKOFF_BASE_SECONDS = 2.0
    BACKOFF_MAX_SECONDS = 60.0

    # Browser-like UA tends to keep Cloudflare happy. The doc explicitly
    # says NOT to swap in TLS-impersonation tooling on first contact —
    # we ride on httpx defaults and a plausible UA.
    USER_AGENT = (
        "Mozilla/5.0 (compatible; legal-sourcing-research/0.1; "
        "+contact: amrosen@bowstreetllc.com)"
    )

    def _default_headers(self) -> dict[str, str]:
        # Polite, browser-shaped header set. No API key needed.
        return {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

    # ---- URL builders --------------------------------------------------

    def state_index_url(self) -> str:
        return f"{self.BASE_URL}/find-attorneys/"

    def state_url(self, state_slug: str) -> str:
        return f"{self.BASE_URL}/by-location/{state_slug}-lawyers/"

    def city_url(self, *, city_slug: str, state_slug: str, page: int = 1) -> str:
        base = f"{self.BASE_URL}/all-lawyers/{city_slug}/{state_slug}/"
        return base if page == 1 else f"{base}?page={page}"


__all__ = [
    "MartindaleScraper",
    "is_path_allowed",
    "_FORBIDDEN_PATH_PREFIXES",
]
