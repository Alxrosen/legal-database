"""Base scraper class.

Each source's scraper subclasses :class:`BaseScraper` and overrides at
minimum :attr:`SOURCE_NAME` and :attr:`BASE_URL`. Optional class-level
overrides control rate limit, concurrency, robots.txt enforcement, and
retry budget.

Responsibilities (do not violate the project's layer rules):

  * Fetch raw HTML/JSON.
  * Store the raw payload (gzipped) and a JSON sidecar under
    `data/raw/{source}/{YYYY-MM-DD}/`.
  * Enforce rate limit and concurrency.
  * Check robots.txt before fetching (hard-block by default).
  * Retry on transient failures with exponential backoff + jitter.

NOT this layer's job:

  * Parsing HTML/JSON into structured data — that lives in `parsers/`.
  * Writing to the database — that lives in `pipelines/`.

Pseudocode for a subclass:

    class AzBarScraper(BaseScraper):
        SOURCE_NAME = "az_bar"
        BASE_URL = "https://azbar.example.com"
        RATE_LIMIT_RPS = 2.0    # AZ bar is slow; be polite
        WORKERS = 2             # match the RPS, don't overcommit
        ROBOTS_POLICY = "warn"  # "warn" | "block" | "ignore"

        def iter_target_urls(self):
            # Yield URLs to fetch. The base class handles fetch/store/retry.
            yield from self._enumerate_member_directory()

        def _enumerate_member_directory(self):
            # source-specific URL discovery logic
            ...

Then in a pipeline:

    scraper = AzBarScraper()
    scraper.scrape_all()       # multi-threaded fetch + store
    # Parsers read from data/raw/az_bar/{date}/ independently.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import random
import time
import urllib.robotparser
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

import httpx

from legal_sourcing.config import get_settings
from legal_sourcing.scrapers._rate_limiter import RateLimiter
from legal_sourcing.utils.logging import get_logger

log = get_logger(__name__)


class ScrapeError(RuntimeError):
    """Raised when a fetch cannot be completed after all retries."""


class RobotsDisallowedError(ScrapeError):
    """Raised when robots.txt forbids a URL and RESPECT_ROBOTS is True."""


class BaseScraper:
    # ---- Subclass must set ---------------------------------------------
    SOURCE_NAME: str = ""  # e.g. "az_bar". Becomes the data/raw/<dir>.
    BASE_URL: str = ""  # used for robots.txt lookup and logging.

    # ---- Optional per-source overrides --------------------------------
    # None -> fall back to Settings (see config.py).
    RATE_LIMIT_RPS: float | None = None
    # Token-bucket burst capacity. None -> defaults to max(1, RPS) inside
    # the limiter, i.e. ~1 second of full-rate headroom after idle.
    BURST_CAPACITY: float | None = None
    # Optional polite-startup ramp. When INITIAL_RATE_LIMIT_RPS is set
    # AND RATE_RAMP_SECONDS > 0, the RPS linearly interpolates from
    # INITIAL_RATE_LIMIT_RPS to RATE_LIMIT_RPS over RATE_RAMP_SECONDS
    # before settling at the target rate.
    INITIAL_RATE_LIMIT_RPS: float | None = None
    RATE_RAMP_SECONDS: float = 0.0
    WORKERS: int | None = None
    USER_AGENT: str | None = None
    TIMEOUT_SECONDS: float | None = None

    # robots.txt policy. Default is "warn": still check robots.txt, log
    # a warning when a URL is disallowed, but proceed with the fetch.
    # Subclasses opt in to "block" for sources where we want hard
    # enforcement, or "ignore" to skip the robots fetch entirely.
    ROBOTS_POLICY: str = "warn"  # one of: "warn", "block", "ignore"

    MAX_RETRIES: int = 3
    BACKOFF_BASE_SECONDS: float = 1.0
    BACKOFF_MAX_SECONDS: float = 60.0

    # Content-type sniffing for the raw filename extension.
    _EXTENSION_BY_CONTENT_TYPE: ClassVar[dict[str, str]] = {
        "application/json": "json",
        "text/html": "html",
        "text/xml": "xml",
        "application/xml": "xml",
    }

    def __init__(self) -> None:
        if not self.SOURCE_NAME:
            raise ValueError(f"{type(self).__name__} must set SOURCE_NAME")
        if not self.BASE_URL:
            raise ValueError(f"{type(self).__name__} must set BASE_URL")
        if self.ROBOTS_POLICY not in ("warn", "block", "ignore"):
            raise ValueError(
                f"{type(self).__name__}.ROBOTS_POLICY must be one of "
                f"'warn', 'block', 'ignore'; got {self.ROBOTS_POLICY!r}"
            )

        s = get_settings()
        self._rps = self.RATE_LIMIT_RPS or s.rate_limit_rps
        self._workers = self.WORKERS or s.max_workers
        self._user_agent = self.USER_AGENT or s.user_agent
        self._timeout = self.TIMEOUT_SECONDS or s.request_timeout_seconds
        self._raw_root: Path = s.raw_data_dir

        self._rate_limiter = RateLimiter(
            self._rps,
            burst=self.BURST_CAPACITY,
            initial_rps=self.INITIAL_RATE_LIMIT_RPS,
            ramp_seconds=self.RATE_RAMP_SECONDS,
        )
        # Merge UA with subclass-supplied default headers (e.g. API key,
        # Referer). Subclass values override UA if there's a collision.
        merged_headers = {"User-Agent": self._user_agent, **self._default_headers()}
        self._client = httpx.Client(
            headers=merged_headers,
            timeout=self._timeout,
            follow_redirects=True,
        )
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}

    def _default_headers(self) -> dict[str, str]:
        """Subclass hook for per-source default headers (API keys, Referer,
        Accept tweaks). Returns {} by default."""
        return {}

    def _check_for_block_response(self, response: httpx.Response) -> None:
        """Subclass hook for soft-block detection. Override to inspect a
        2xx response and raise (typically `ScrapeError`) if the body is
        actually an anti-bot challenge or interstitial. The default is
        a no-op; sources behind CDNs / WAFs should subclass."""
        return None

    # ---- Public API ----------------------------------------------------

    def iter_target_urls(self) -> Iterable[str]:
        """Subclass override. Yields URLs to fetch in this scrape pass."""
        raise NotImplementedError

    def scrape_all(self) -> list[Path]:
        """Fetch every URL from `iter_target_urls()` concurrently.

        Returns the list of raw-payload Paths written. Errors per URL
        are logged and skipped — one bad URL does not abort the run.
        """
        target_dir = self._date_partition_dir(date.today())
        target_dir.mkdir(parents=True, exist_ok=True)

        log.info(
            "scrape.start",
            source=self.SOURCE_NAME,
            workers=self._workers,
            rps=self._rps,
            out_dir=str(target_dir),
        )

        written: list[Path] = []
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = {
                pool.submit(self._fetch_and_store, url, target_dir): url
                for url in self.iter_target_urls()
            }
            for fut in as_completed(futures):
                url = futures[fut]
                try:
                    path = fut.result()
                except Exception as exc:
                    log.error("scrape.url_failed", url=url, error=str(exc))
                    continue
                if path is not None:
                    written.append(path)

        log.info("scrape.done", source=self.SOURCE_NAME, count=len(written))
        return written

    def fetch_one(
        self,
        url: str,
        *,
        method: str = "GET",
        json_body: dict | None = None,
        data: dict | None = None,
        params: dict | None = None,
        bucket: str | None = None,
        filename: str | None = None,
    ) -> Path | None:
        """Single-URL fetch + store. The base building block.

        Parameters
        ----------
        url
            Absolute URL to fetch.
        method
            HTTP method. Defaults to GET. The AZ Bar list endpoint, for
            example, requires POST with query params.
        json_body
            JSON body for POST/PUT/PATCH. Ignored for GET.
        data
            Form-encoded body (application/x-www-form-urlencoded) for
            POST. Used by HTML form endpoints (e.g. state-bar searches).
        params
            Query-string params, appended to the URL for any method.
        bucket
            Optional subdirectory under the date partition. Useful when
            a single source has structurally distinct payload classes
            (reference / list / detail).
        filename
            Optional stem for the stored payload. If omitted, defaults
            to sha256(url)[:16]. Use a stable name when re-fetches
            should overwrite (e.g. "page_0001").

        Returns the path of the gzipped payload on disk.
        """
        target_dir = self._date_partition_dir(date.today())
        if bucket:
            target_dir = target_dir / bucket
        target_dir.mkdir(parents=True, exist_ok=True)
        return self._fetch_and_store(
            url,
            target_dir,
            method=method,
            json_body=json_body,
            data=data,
            params=params,
            filename=filename,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BaseScraper:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- Internals -----------------------------------------------------

    def _fetch_and_store(
        self,
        url: str,
        target_dir: Path,
        *,
        method: str = "GET",
        json_body: dict | None = None,
        data: dict | None = None,
        params: dict | None = None,
        filename: str | None = None,
    ) -> Path | None:
        if self.ROBOTS_POLICY != "ignore" and not self._robots_allows(url):
            if self.ROBOTS_POLICY == "block":
                log.warning("scrape.robots_blocked", url=url, policy="block")
                raise RobotsDisallowedError(url)
            # "warn" (default): record the violation but proceed.
            log.warning(
                "scrape.robots_disallowed",
                url=url,
                policy=self.ROBOTS_POLICY,
                note="proceeding despite robots disallow",
            )

        response = self._fetch_with_retries(
            url, method=method, json_body=json_body, data=data, params=params
        )
        path = self._store_payload(url, response, target_dir, filename=filename)
        log.debug(
            "scrape.fetched",
            url=url,
            method=method,
            status=response.status_code,
            bytes=len(response.content),
            path=str(path),
        )
        return path

    def _fetch_with_retries(
        self,
        url: str,
        *,
        method: str = "GET",
        json_body: dict | None = None,
        data: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.MAX_RETRIES + 2):  # initial + retries
            self._rate_limiter.acquire()
            try:
                response = self._client.request(
                    method, url, json=json_body, data=data, params=params
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                wait = self._compute_backoff(attempt, retry_after=None)
                log.warning(
                    "scrape.transport_error",
                    url=url,
                    attempt=attempt,
                    sleep=wait,
                    error=str(exc),
                )
                time.sleep(wait)
                continue

            if response.status_code < 400:
                # Subclass hook for soft-block detection (e.g. Cloudflare
                # challenge pages that return 200 with challenge HTML).
                # The default is a no-op; FindLaw overrides to raise.
                self._check_for_block_response(response)
                return response

            # Retryable status codes: 429 (rate limited) + 5xx
            if response.status_code == 429 or 500 <= response.status_code < 600:
                wait = self._compute_backoff(
                    attempt, retry_after=response.headers.get("Retry-After")
                )
                log.warning(
                    "scrape.http_retryable",
                    url=url,
                    status=response.status_code,
                    attempt=attempt,
                    sleep=wait,
                )
                if attempt > self.MAX_RETRIES:
                    raise ScrapeError(
                        f"{method} {url} -> {response.status_code} after {attempt} attempts"
                    )
                time.sleep(wait)
                continue

            # Non-retryable error (4xx other than 429). Include a body
            # excerpt so the caller can see what the server said —
            # invaluable for diagnosing 401/403/422.
            body_excerpt = response.text[:300] if response.text else ""
            raise ScrapeError(
                f"{method} {url} -> {response.status_code} (non-retryable)"
                + (f"\nBody: {body_excerpt!r}" if body_excerpt else "")
            )

        # Exhausted retries on transport errors.
        raise ScrapeError(f"{method} {url} failed after retries") from last_exc

    def _compute_backoff(self, attempt: int, retry_after: str | None) -> float:
        # Honor Retry-After when present (RFC 7231: seconds or HTTP-date).
        if retry_after:
            try:
                return min(float(retry_after), self.BACKOFF_MAX_SECONDS)
            except ValueError:
                pass  # HTTP-date form ignored; fall through to expo backoff.
        # Full-jitter exponential backoff.
        ceiling = min(
            self.BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
            self.BACKOFF_MAX_SECONDS,
        )
        return random.uniform(0, ceiling)

    def _robots_allows(self, url: str) -> bool:
        host = urlparse(url).netloc
        if not host:
            return False
        if host not in self._robots_cache:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = f"{urlparse(url).scheme or 'https'}://{host}/robots.txt"
            try:
                resp = self._client.get(robots_url)
                if resp.status_code >= 400:
                    # No robots.txt -> allow per RFC 9309 convention.
                    rp.parse([])
                else:
                    rp.parse(resp.text.splitlines())
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                log.warning("scrape.robots_fetch_failed", host=host, error=str(exc))
                rp.parse([])  # treat as unrestricted
            self._robots_cache[host] = rp
        return self._robots_cache[host].can_fetch(self._user_agent, url)

    def _store_payload(
        self,
        url: str,
        response: httpx.Response,
        target_dir: Path,
        *,
        filename: str | None = None,
    ) -> Path:
        # If filename provided, use it as the stem; otherwise hash the URL.
        # Either way the stored file is gzipped with a .{ext}.gz tail and a
        # JSON sidecar with the same stem.
        stem = filename or hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        ext = self._extension_for(response)
        gz_path = target_dir / f"{stem}.{ext}.gz"
        json_path = target_dir / f"{stem}.json"

        # Body
        with gzip.open(gz_path, "wb") as f:
            f.write(response.content)

        # Sidecar
        sidecar = {
            "url": url,
            "final_url": str(response.url),
            "status": response.status_code,
            "fetched_at": datetime.now(UTC).isoformat(),
            "headers": {k: v for k, v in response.headers.items()},
            "source": self.SOURCE_NAME,
            "body_path": gz_path.name,
            "body_sha256": hashlib.sha256(response.content).hexdigest(),
        }
        json_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")

        return gz_path

    def _extension_for(self, response: httpx.Response) -> str:
        ct = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return self._EXTENSION_BY_CONTENT_TYPE.get(ct, "bin")

    def _date_partition_dir(self, d: date) -> Path:
        return self._raw_root / self.SOURCE_NAME / d.isoformat()
