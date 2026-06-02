"""Arizona State Bar member-directory scraper.

Targets the JSON API at api-proxy.azbar.org described in
docs/data_sources/az_bar_reference.md. This is a scaffold — concrete
scrape phases (reference, list, detail) are filled in after the
reconnaissance script confirms endpoint shapes.

Architecture notes
------------------

Disk layout (lives under data/raw/az_bar/{YYYY-MM-DD}/):

    reference/
        specializations.json.gz, abs.json.gz, states.json.gz, ...
    list/
        page_0001.json.gz, page_0002.json.gz, ...
    detail/
        {EntityNumber}.json.gz

Stable filenames within a date partition mean re-running on the same
day overwrites yesterday's-shape mistakes cleanly. A new scrape day
gets a new partition; we never overwrite across dates.

Concurrency: 10 workers, 15 RPS sustained cap (per the reference
doc's recommended starting point). Tune empirically; back off on 429
or sustained 5xx. The token bucket has burst capacity = 15 so the
first second after idle can fire freely.

robots.txt: warn-and-proceed (project default). The AZ Bar API is a
proxy in front of an internal service; bypassing its robots is the
documented approach for this source. We log every disallowed URL but
do not block.
"""

from __future__ import annotations

from legal_sourcing.config import get_settings
from legal_sourcing.scrapers.base import BaseScraper

# Static configuration for the AZ Bar API — see reference doc for context.
_API_HOST = "https://api-proxy.azbar.org"


class AZBarApiPasswordMissingError(RuntimeError):
    """Raised at scraper init when AZBAR_API_PASSWORD is not configured."""


class AZBarApiPasswordRotatedError(RuntimeError):
    """Raised when the API returns 401/403, which is the documented
    symptom of the Password header having rotated. Stops the scrape so
    the operator can re-capture and update .env.
    """


class AZBarScraper(BaseScraper):
    SOURCE_NAME = "az_bar"
    BASE_URL = _API_HOST

    # Polite-startup ramp, relaxed for production scale. Begin at 6 RPS
    # with a 5-token burst, linearly ramp to 20 RPS over 60s. Workers
    # at 12. Bumped from the original (3 RPS -> 15 RPS over 120s,
    # 10 workers) once recon confirmed the API tolerates the documented
    # 15 RPS cap. Watch for 429s during the first long sweep; back off
    # the target if they appear.
    RATE_LIMIT_RPS = 20.0
    INITIAL_RATE_LIMIT_RPS = 6.0
    RATE_RAMP_SECONDS = 60.0
    BURST_CAPACITY = 5.0
    WORKERS = 12

    # The Password header is a static UUID; we're effectively
    # impersonating the official front-end. Hard-blocking on robots
    # for this source isn't appropriate (the API is proxy-fronted and
    # robots is aimed at general crawlers). Stay on the project default.
    ROBOTS_POLICY = "warn"

    MAX_RETRIES = 3
    BACKOFF_BASE_SECONDS = 1.0
    BACKOFF_MAX_SECONDS = 60.0

    # AZ Bar API endpoints (built relative to BASE_URL by helpers).
    SEARCH_PATH = "/MemberSearch/Search"
    SPECIALIZATIONS_PATH = "/MemberSearch/Specializations"
    ABS_PATH = "/MemberSearch/ABS"
    STATES_PATH = "/MemberSearch/States"
    COUNTIES_PATH = "/MemberSearch/Counties"
    JURISDICTIONS_PATH = "/MemberSearch/Jurisdictions"
    LANGUAGES_PATH = "/MemberSearch/Languages"
    LAW_SCHOOLS_PATH = "/MemberSearch/LawSchools"
    SECTIONS_PATH = "/MemberSearch/Sections"

    def __init__(self) -> None:
        # Resolve secret BEFORE calling super().__init__ — _default_headers
        # is consulted during super().__init__ to build the httpx client.
        password = get_settings().azbar_api_password.strip()
        if not password:
            raise AZBarApiPasswordMissingError(
                "AZBAR_API_PASSWORD is not set. "
                "Capture the current value from DevTools per "
                "docs/data_sources/az_bar_reference.md and add it to .env."
            )
        self._password = password
        super().__init__()

    def _default_headers(self) -> dict[str, str]:
        # Headers matched against a real browser request captured from
        # DevTools — see docs/data_sources/az_bar_reference.md for the
        # capture procedure. Minimum set:
        #   * Password — the static UUID, the actual auth.
        #   * Userid: publictools — application-identity header. Missing
        #     this returns 401 even when Password is correct.
        #   * Origin/Referer — browsers send both; the proxy uses them
        #     to authorize same-site CORS calls.
        #   * Accept/Content-Type — standard.
        #
        # NOT included (browsers don't send these for this API):
        #   * X-Requested-With — was added speculatively; removed.
        #     Sending it may flag the call as non-browser.
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json; charset=UTF-8",
            "Password": self._password,
            "Userid": "publictools",
            "Origin": "https://www.azbar.org",
            "Referer": "https://www.azbar.org/",
        }

    # ---- URL builders --------------------------------------------------

    def list_url(
        self,
        *,
        page: int,
        page_size: int = 25,
        shuffle: bool = False,
        seed: str = "null",
        specialization_code: str | None = None,
    ) -> str:
        # NOTE: the reference doc shows trailing literal `{}` and `?{}`
        # tokens (e.g. "?PageSize=25&...&Seed=null&{}"). Those are
        # JavaScript template artifacts from the captured URLs and get
        # percent-encoded by httpx into %7B%7D, which some WAFs reject.
        # We omit them and send clean URLs.
        params = (
            f"?PageSize={page_size}&Page={page}"
            f"&RequestorEntityNumber=undefined"
            f"&Shuffle={'true' if shuffle else 'false'}"
            f"&Seed={seed}"
        )
        if specialization_code:
            params += f"&CertifiedSpecializationCode={specialization_code}"
        return f"{self.BASE_URL}{self.SEARCH_PATH}/{params}"

    def detail_url(self, entity_number: int | str) -> str:
        return (
            f"{self.BASE_URL}{self.SEARCH_PATH}"
            f"?EntityNumber={entity_number}&RequestorEntityNumber=undefined"
        )

    def reference_url(self, path: str, *, include_inactive: bool = False) -> str:
        if include_inactive:
            return f"{self.BASE_URL}{path}?IncludeInactive=true"
        return f"{self.BASE_URL}{path}"

    # ---- iter_target_urls is NOT implemented yet ----------------------
    # The reference-then-list-then-detail orchestration belongs in a
    # pipeline (M4-pipeline). Scraper exposes the building blocks
    # (fetch_one with method/bucket/filename) and the URL builders.
