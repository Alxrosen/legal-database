"""FindLaw directory scraper.

Targets the HTML site at `lawyers.findlaw.com` per
docs/data_sources/findlaw_reference.md. Status: **scaffold + recon
phase only** — parser and pipeline work intentionally deferred until
the recon script has verified the doc's structural claims.

Posture decisions from the reference doc:

* **Browser-shaped User-Agent.** The project's identified UA would
  invite Cloudflare bot scoring. Use a current Chrome-on-Windows UA;
  no "bot" / "research" / contact string.
* **Conservative rate.** 0.33 RPS sustained, burst=1, 1 worker.
  Single-threaded recon — no concurrency until we know the source
  tolerates more.
* **Cloudflare challenges are fatal.** Detect challenge responses
  (HTTP 403/429/503, AND 2xx with challenge-page HTML) and abort
  the run. Do not retry, do not try to defeat the challenge.
"""

from __future__ import annotations

import httpx

from legal_sourcing.scrapers.base import BaseScraper, ScrapeError

# Markers that identify a Cloudflare challenge page even when the
# response status is 200. Case-insensitive substring match.
_CLOUDFLARE_CHALLENGE_MARKERS: tuple[str, ...] = (
    "cf-mitigated",
    "__cf_chl_",
    "just a moment",
    "attention required",
    "checking your browser",
    "cloudflare ray id",
    "cf_chl_managed",
)


class FindLawCloudflareChallenge(ScrapeError):
    """Raised when FindLaw returns a Cloudflare challenge page. Stops
    the run; the operator decides whether to back off, change posture,
    or switch tooling. Per the data-source doc, retrying is a bad
    idea — it burns retry budget and may escalate the response.
    """


class FindLawScraper(BaseScraper):
    SOURCE_NAME = "findlaw"
    BASE_URL = "https://lawyers.findlaw.com"

    # Conservative defaults — doc-recommended.
    RATE_LIMIT_RPS = 0.33  # one request every ~3 seconds
    INITIAL_RATE_LIMIT_RPS = 0.2  # a slightly slower first minute
    RATE_RAMP_SECONDS = 60.0
    BURST_CAPACITY = 1.0  # no bursting
    WORKERS = 1  # single-threaded recon

    ROBOTS_POLICY = "warn"  # project default
    MAX_RETRIES = 3  # Cloudflare challenges aren't retryable; budget low

    # Browser UA — do NOT include "bot", "research", or a contact in
    # the UA for this source. Cloudflare bot scoring will penalize
    # identified bots even when robots.txt allows.
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def _default_headers(self) -> dict[str, str]:
        # Browser-shaped header set. Same fields a real Chrome XHR sends.
        return {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def _check_for_block_response(self, response: httpx.Response) -> None:
        """Inspect a 2xx response for Cloudflare-challenge markers.

        The body is decoded only if Content-Type looks like HTML to
        avoid spending cycles on binary fixtures. Markers are matched
        case-insensitively. On a hit we raise
        FindLawCloudflareChallenge so the run aborts with a clear
        cause.
        """
        ctype = (response.headers.get("Content-Type") or "").lower()
        if "html" not in ctype:
            return
        # Quick check on the raw body (cheaper than the full decode for
        # very large pages). Use the bytes directly.
        body = response.content[:8000].lower()
        for marker in _CLOUDFLARE_CHALLENGE_MARKERS:
            if marker.encode("ascii") in body:
                raise FindLawCloudflareChallenge(
                    f"Cloudflare challenge response detected on {response.url} "
                    f"(marker: {marker!r}). See "
                    f"docs/data_sources/findlaw_reference.md §Cloudflare protection."
                )

    # ---- URL builders --------------------------------------------------

    def root_url(self) -> str:
        return f"{self.BASE_URL}/"

    def practice_area_index_url(self) -> str:
        return f"{self.BASE_URL}/legal-issues/"

    def practice_area_state_url(self, *, practice_area_slug: str, state_slug: str) -> str:
        return f"{self.BASE_URL}/{practice_area_slug}/{state_slug}/"

    def practice_area_city_url(
        self,
        *,
        practice_area_slug: str,
        state_slug: str,
        city_slug: str,
        page: int = 1,
        extra_params: str = "",
    ) -> str:
        base = f"{self.BASE_URL}/{practice_area_slug}/{state_slug}/{city_slug}/"
        qs_parts = []
        if extra_params:
            qs_parts.append(extra_params.lstrip("?&"))
        if page > 1:
            qs_parts.append(f"page={page}")
        return base + ("?" + "&".join(qs_parts) if qs_parts else "")


__all__ = [
    "_CLOUDFLARE_CHALLENGE_MARKERS",
    "FindLawCloudflareChallenge",
    "FindLawScraper",
]
