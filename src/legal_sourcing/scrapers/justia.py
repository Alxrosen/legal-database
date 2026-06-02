"""Justia Lawyer Directory scraper.

Targets `https://lawyers.justia.com` per docs/data_sources/justia.md.

Posture (CONFIRMED 2026-06-02):

* **Cloudflare-fronted, passes with a full browser header set.** A
  minimal header set draws a 403 "Just a moment" challenge; the full
  set (`sec-ch-ua`, `Sec-Fetch-*`, `Upgrade-Insecure-Requests`) returns
  200. So we send browser-shaped headers and a Chrome UA. We still
  treat a challenge body as a fatal `JustiaCloudflareChallenge` — if
  Cloudflare tightens, STOP, do not try to defeat it.
* **Polite rate.** 0.5 RPS sustained, 0.33→0.5 ramp, burst 1, single
  worker. First 403 / interstitial → stop and revisit.
* **robots.txt** allows `/lawyers/...`; project default `warn` policy.
"""

from __future__ import annotations

import httpx

from legal_sourcing.scrapers.base import BaseScraper, ScrapeError

# Markers that identify a Cloudflare challenge page even on a 2xx.
_CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "cf-mitigated",
    "attention required",
    "cf_chl_",
    "checking your browser",
)


class JustiaCloudflareChallenge(ScrapeError):
    """Raised when Justia returns a Cloudflare challenge page. Stops the
    run; the operator decides whether to back off or change posture.
    Per the data-source doc, retrying is a bad idea."""


class JustiaScraper(BaseScraper):
    SOURCE_NAME = "justia"
    # The directory is canonically served from www.justia.com;
    # lawyers.justia.com 301-redirects every request there. Target www
    # directly to skip the redirect hop. www robots.txt is fully open.
    BASE_URL = "https://www.justia.com"

    # Cloudflare-fronted but cooperative with proper headers. Stay polite.
    RATE_LIMIT_RPS = 0.5
    INITIAL_RATE_LIMIT_RPS = 0.33
    RATE_RAMP_SECONDS = 30.0
    BURST_CAPACITY = 1.0
    WORKERS = 1

    ROBOTS_POLICY = "warn"
    MAX_RETRIES = 3  # challenges aren't retryable; keep the budget low

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def _default_headers(self) -> dict[str, str]:
        # FULL browser header set — the minimal set draws a Cloudflare
        # challenge; this set passes. Do not trim without re-confirming.
        return {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": ('"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'),
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
                raise JustiaCloudflareChallenge(
                    f"Cloudflare challenge detected on {response.url} "
                    f"(marker: {marker!r}). See "
                    f"docs/data_sources/justia.md."
                )

    # ---- URL builders --------------------------------------------------

    def state_url(self, state_slug: str, *, page: int = 1) -> str:
        base = f"{self.BASE_URL}/lawyers/{state_slug}"
        return base if page <= 1 else f"{base}?page={page}"

    def city_url(self, *, state_slug: str, city_slug: str, page: int = 1) -> str:
        base = f"{self.BASE_URL}/lawyers/{state_slug}/{city_slug}"
        return base if page <= 1 else f"{base}?page={page}"

    def practice_area_state_url(
        self, *, practice_area_slug: str, state_slug: str, page: int = 1
    ) -> str:
        base = f"{self.BASE_URL}/lawyers/{practice_area_slug}/{state_slug}"
        return base if page <= 1 else f"{base}?page={page}"


__all__ = ["_CHALLENGE_MARKERS", "JustiaCloudflareChallenge", "JustiaScraper"]
