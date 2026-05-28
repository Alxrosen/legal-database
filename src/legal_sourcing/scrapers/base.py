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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable
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
    _EXTENSION_BY_CONTENT_TYPE: dict[str, str] = {
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

        self._rate_limiter = RateLimiter(self._rps, burst=self.BURST_CAPACITY)
        self._client = httpx.Client(
            headers={"User-Agent": self._user_agent},
            timeout=self._timeout,
            follow_redirects=True,
        )
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}

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
                except Exception as exc:  # noqa: BLE001
                    log.error("scrape.url_failed", url=url, error=str(exc))
                    continue
                if path is not None:
                    written.append(path)

        log.info("scrape.done", source=self.SOURCE_NAME, count=len(written))
        return written

    def fetch_one(self, url: str) -> Path | None:
        """Single-URL fetch + store. Useful for testing and ad-hoc runs."""
        target_dir = self._date_partition_dir(date.today())
        target_dir.mkdir(parents=True, exist_ok=True)
        return self._fetch_and_store(url, target_dir)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BaseScraper":
        return self

    def __exit__(self, *exc_info) -> None:  # noqa: ANN001
        self.close()

    # ---- Internals -----------------------------------------------------

    def _fetch_and_store(self, url: str, target_dir: Path) -> Path | None:
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

        response = self._fetch_with_retries(url)
        path = self._store_payload(url, response, target_dir)
        log.debug(
            "scrape.fetched",
            url=url,
            status=response.status_code,
            bytes=len(response.content),
            path=str(path),
        )
        return path

    def _fetch_with_retries(self, url: str) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.MAX_RETRIES + 2):  # initial + retries
            self._rate_limiter.acquire()
            try:
                response = self._client.get(url)
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
                        f"GET {url} -> {response.status_code} after {attempt} attempts"
                    )
                time.sleep(wait)
                continue

            # Non-retryable error (4xx other than 429).
            raise ScrapeError(f"GET {url} -> {response.status_code} (non-retryable)")

        # Exhausted retries on transport errors.
        raise ScrapeError(f"GET {url} failed after retries") from last_exc

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
    ) -> Path:
        # Stable filename so repeat fetches overwrite the same slot
        # within a day. Different dates produce different slots.
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        ext = self._extension_for(response)
        gz_path = target_dir / f"{digest}.{ext}.gz"
        json_path = target_dir / f"{digest}.json"

        # Body
        with gzip.open(gz_path, "wb") as f:
            f.write(response.content)

        # Sidecar
        sidecar = {
            "url": url,
            "final_url": str(response.url),
            "status": response.status_code,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
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
