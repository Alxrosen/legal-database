"""Base scraper behavior tests — uses respx to mock httpx.

These exercise the retry / robots / payload-storage behavior without
hitting the network.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import httpx
import pytest
import respx

from legal_sourcing.scrapers.base import (
    BaseScraper,
    RobotsDisallowedError,
    ScrapeError,
)


class _DummyScraper(BaseScraper):
    SOURCE_NAME = "_test"
    BASE_URL = "https://example.com"
    RATE_LIMIT_RPS = 100.0  # don't slow tests down
    WORKERS = 2
    MAX_RETRIES = 2
    BACKOFF_BASE_SECONDS = 0.001  # near-zero so tests stay fast
    BACKOFF_MAX_SECONDS = 0.01

    def __init__(self, raw_root: Path):
        # Override settings.raw_data_dir for this instance.
        super().__init__()
        self._raw_root = raw_root


def _make(tmp_path: Path) -> _DummyScraper:
    return _DummyScraper(raw_root=tmp_path / "raw")


@respx.mock
def test_successful_fetch_stores_gzip_and_sidecar(tmp_path):
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    body = b"<html><body>hello</body></html>"
    respx.get("https://example.com/firms/1").mock(
        return_value=httpx.Response(
            200, content=body, headers={"Content-Type": "text/html"}
        )
    )

    scraper = _make(tmp_path)
    path = scraper.fetch_one("https://example.com/firms/1")
    assert path is not None and path.exists()
    assert path.suffix == ".gz"
    # Body matches.
    with gzip.open(path, "rb") as f:
        assert f.read() == body
    # Sidecar matches.
    sidecar = json.loads(path.with_suffix("").with_suffix(".json").read_text())
    assert sidecar["status"] == 200
    assert sidecar["url"] == "https://example.com/firms/1"
    assert sidecar["source"] == "_test"
    scraper.close()


@respx.mock
def test_robots_blocks_disallowed_path(tmp_path):
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(
            200, text="User-agent: *\nDisallow: /private/\n"
        )
    )
    # No mock for /private/x — robots should block before any GET happens.

    scraper = _make(tmp_path)
    with pytest.raises(RobotsDisallowedError):
        scraper.fetch_one("https://example.com/private/x")
    scraper.close()


@respx.mock
def test_429_retried_then_succeeds(tmp_path):
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    route = respx.get("https://example.com/data")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}, text=""),
        httpx.Response(200, content=b"ok", headers={"Content-Type": "text/html"}),
    ]

    scraper = _make(tmp_path)
    path = scraper.fetch_one("https://example.com/data")
    assert path is not None and path.exists()
    assert route.call_count == 2
    scraper.close()


@respx.mock
def test_non_retryable_4xx_raises(tmp_path):
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    respx.get("https://example.com/nope").mock(
        return_value=httpx.Response(404, text="")
    )

    scraper = _make(tmp_path)
    with pytest.raises(ScrapeError):
        scraper.fetch_one("https://example.com/nope")
    scraper.close()


@respx.mock
def test_5xx_exhausts_retries_then_raises(tmp_path):
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    respx.get("https://example.com/broken").mock(
        return_value=httpx.Response(503, headers={"Retry-After": "0"}, text="")
    )

    scraper = _make(tmp_path)
    with pytest.raises(ScrapeError):
        scraper.fetch_one("https://example.com/broken")
    scraper.close()
