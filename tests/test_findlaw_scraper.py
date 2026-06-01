"""FindLaw scraper unit tests — focused on the Cloudflare-detection
hook and the URL builders. The recon script is the integration test.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from legal_sourcing.scrapers.findlaw import (
    FindLawCloudflareChallenge,
    FindLawScraper,
    _CLOUDFLARE_CHALLENGE_MARKERS,
)


class _TestFindLawScraper(FindLawScraper):
    """Test fixture: keep the FindLaw config but point storage at tmp
    and disable the polite ramp so tests stay fast.
    """

    RATE_LIMIT_RPS = 100.0
    INITIAL_RATE_LIMIT_RPS = 100.0
    RATE_RAMP_SECONDS = 0.0
    BURST_CAPACITY = 5.0
    MAX_RETRIES = 1
    BACKOFF_BASE_SECONDS = 0.001
    BACKOFF_MAX_SECONDS = 0.01

    def __init__(self, raw_root: Path):
        super().__init__()
        self._raw_root = raw_root


def _make(tmp_path):
    return _TestFindLawScraper(raw_root=tmp_path / "raw")


def test_url_builders():
    s = FindLawScraper()
    try:
        assert s.root_url() == "https://lawyers.findlaw.com/"
        assert (
            s.practice_area_index_url()
            == "https://lawyers.findlaw.com/legal-issues/"
        )
        assert s.practice_area_state_url(
            practice_area_slug="dui-dwi", state_slug="arizona"
        ) == "https://lawyers.findlaw.com/dui-dwi/arizona/"
        # page=1 omits the page param.
        assert s.practice_area_city_url(
            practice_area_slug="dui-dwi", state_slug="arizona", city_slug="phoenix"
        ) == "https://lawyers.findlaw.com/dui-dwi/arizona/phoenix/"
        # page>1 appends ?page=N.
        assert s.practice_area_city_url(
            practice_area_slug="dui-dwi",
            state_slug="arizona",
            city_slug="phoenix",
            page=3,
        ) == "https://lawyers.findlaw.com/dui-dwi/arizona/phoenix/?page=3"
        # extra_params combines correctly.
        assert s.practice_area_city_url(
            practice_area_slug="dui-dwi",
            state_slug="arizona",
            city_slug="phoenix",
            page=2,
            extra_params="keyword=DUI",
        ) == "https://lawyers.findlaw.com/dui-dwi/arizona/phoenix/?keyword=DUI&page=2"
    finally:
        s.close()


def test_user_agent_is_browser_shaped():
    """Doc explicitly says: do NOT use the project's identified UA
    on FindLaw — Cloudflare will score that as a bot.
    """
    ua = FindLawScraper.USER_AGENT
    forbidden = ("bot", "research", "contact:", "+contact", "scraper")
    for tok in forbidden:
        assert tok.lower() not in ua.lower(), tok
    # Must look like a real browser UA.
    assert ua.startswith("Mozilla/5.0")
    assert "Chrome/" in ua


@respx.mock
@pytest.mark.parametrize(
    "marker",
    _CLOUDFLARE_CHALLENGE_MARKERS,
)
def test_cloudflare_challenge_in_200_aborts(tmp_path, marker):
    """Even on a 2xx response, finding a challenge marker in the body
    must raise FindLawCloudflareChallenge — not silently return."""
    respx.get("https://lawyers.findlaw.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    body = f"<html><head><title>x</title></head><body>{marker}</body></html>".encode()
    respx.get("https://lawyers.findlaw.com/whatever/").mock(
        return_value=httpx.Response(
            200, content=body, headers={"Content-Type": "text/html"}
        )
    )
    scraper = _make(tmp_path)
    try:
        with pytest.raises(FindLawCloudflareChallenge):
            scraper.fetch_one("https://lawyers.findlaw.com/whatever/")
    finally:
        scraper.close()


@respx.mock
def test_non_html_response_is_not_checked_for_cloudflare(tmp_path):
    """Sitemap XML and other non-HTML responses should pass through
    even if they happen to contain a challenge-marker substring."""
    respx.get("https://lawyers.findlaw.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    body = b"<urlset><url><loc>cf-mitigated-something</loc></url></urlset>"
    respx.get("https://lawyers.findlaw.com/sitemap.xml").mock(
        return_value=httpx.Response(
            200, content=body, headers={"Content-Type": "application/xml"}
        )
    )
    scraper = _make(tmp_path)
    try:
        path = scraper.fetch_one("https://lawyers.findlaw.com/sitemap.xml")
        assert path is not None and path.exists()
    finally:
        scraper.close()
