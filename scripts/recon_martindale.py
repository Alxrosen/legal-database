"""Martindale reconnaissance — fetch a small slice, save fixtures,
confirm the structures match docs/data_sources/martindale.md.

What it does:

1. Fetches the state index page (`/find-attorneys/`).
2. Extracts state links per the doc's Level-1 selector; reports count
   (~50 + DC expected).
3. Fetches one state's city index (default: Alabama).
4. Extracts city links per the doc's Level-2 selector.
5. Fetches one small city's attorney page (default: Abbeville, AL).
6. Extracts attorney "cards" per the doc's Level-3 fields.
7. Picks one firm URL from those cards and fetches the firm profile.
8. Extracts the firm website button per the doc's Level-4 selector.

All raw pages stored under data/raw/martindale/{date}/<bucket>/ via
the base scraper. Pretty-printed JSON summaries written to
tests/fixtures/martindale/recon/ for easy human review and as a
deterministic input for future parser tests.

Failures:
* Cloudflare challenge / 403 / 503 -> stop the run with a clear
  message (per the doc, don't escalate tooling).
* Selector mismatches surface as warnings — the script doesn't abort,
  so we get the maximum diagnostic out of one run.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Make the package importable when run as `uv run python scripts/...`.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from selectolax.parser import HTMLParser  # noqa: E402

from legal_sourcing.scrapers.base import ScrapeError  # noqa: E402
from legal_sourcing.scrapers.martindale import (  # noqa: E402
    MartindaleScraper,
    is_path_allowed,
)
from legal_sourcing.utils.logging import configure_logging, get_logger  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures" / "martindale" / "recon"
log = get_logger(__name__)


def _save_fixture(name: str, data: Any) -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURES_DIR / f"{name}.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def _read_gz_text(path: Path) -> str:
    with gzip.open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


# ---- Per-level extractors ---------------------------------------------


def extract_states(html: str) -> list[dict[str, str]]:
    """Doc §4.1 — state links under the "Browse by States" section.

    The doc's CSS selector relies on `:contains()` (not supported by
    selectolax). Empirically the page also has "Browse by Areas of
    Law" and "Browse by Cities" sections that share the same
    `browse-list__a--grey` class — so a class-only selector returns
    ~109 anchors, only ~65 of which are actual state links.

    Filter pragmatically by URL prefix: state links go to
    `/by-location/{state}-lawyers/`. Anything else (areas-of-law,
    individual cities) is excluded.
    """
    tree = HTMLParser(html)
    anchors = tree.css("ul.browse-list__ul a.browse-list__a--grey")
    out: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for a in anchors:
        href = a.attributes.get("href") or ""
        name = a.text(strip=True)
        if not href or not name:
            continue
        path = urlparse(href).path
        if not path.startswith("/by-location/"):
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        out.append({"name": name, "url": href})
    return out


def extract_cities(html: str) -> list[dict[str, str]]:
    """Doc §4.2 — every city anchor across the alphabet panels."""
    tree = HTMLParser(html)
    out: list[dict[str, str]] = []
    for a in tree.css('div[id^="Panel"] ul.browse-list__ul li.browse-list__li a'):
        href = a.attributes.get("href") or ""
        name = a.text(strip=True)
        if href and name:
            out.append({"name": name, "url": href})
    return out


_ATTORNEY_ID_RE = re.compile(r"-(\d+)/?$")


def _parse_gtm(attr_value: str) -> dict[str, Any] | None:
    if not attr_value:
        return None
    try:
        return json.loads(attr_value)
    except json.JSONDecodeError:
        return None


def extract_attorney_cards(html: str) -> list[dict[str, Any]]:
    """Doc §4.3 — per-card field extraction. Best-effort per the
    'every detail_* field is optional' caveat in §10.
    """
    tree = HTMLParser(html)
    cards = tree.css("div.card.card--attorney, .card.card--attorney, [class*='card--attorney']")
    out: list[dict[str, Any]] = []
    for card in cards:
        rec: dict[str, Any] = {}

        # detail_title -> profile URL, attorney name, source id
        title_a = card.css_first("li.detail_title > a")
        if title_a is not None:
            href = title_a.attributes.get("href") or ""
            rec["attorney_profile_url"] = href
            if href:
                m = _ATTORNEY_ID_RE.search(urlparse(href).path)
                if m:
                    rec["source_attorney_id"] = m.group(1)
            h3 = title_a.css_first("h3")
            if h3 is not None:
                rec["attorney_name"] = h3.text(strip=True)

        # detail_position -> title + firm link
        pos = card.css_first("li.detail_position")
        if pos is not None:
            firm_a = pos.css_first("a.detail_position--office-link")
            if firm_a is not None:
                # Title is the text before the firm anchor.
                pre = (pos.text(strip=True) or "").split(firm_a.text(strip=True))[0]
                title_raw = pre.strip().rstrip(",")
                if title_raw.endswith(" at"):
                    title_raw = title_raw[:-3].strip()
                rec["title_raw"] = title_raw or None
                rec["firm_name_raw"] = firm_a.text(strip=True) or None
                rec["firm_profile_url"] = firm_a.attributes.get("href") or None
                gtm = _parse_gtm(firm_a.attributes.get("data-gtm-tracking") or "")
                if gtm:
                    rec["source_firm_id_martindale"] = gtm.get("firm_id")
            else:
                rec["title_raw"] = pos.text(strip=True) or None

        # detail_location -> address text (parse later)
        loc = card.css_first("li.detail_location")
        if loc is not None:
            rec["location_text"] = loc.text(strip=True) or None

        # contact tel:
        tel = card.css_first("a[href^='tel:']")
        if tel is not None:
            rec["phone"] = (tel.attributes.get("href") or "").replace("tel:", "") or None

        # bio
        bio = card.css_first("li.detail_bio")
        if bio is not None:
            rec["bio_snippet"] = bio.text(strip=True) or None

        # awards
        trophies = card.css_first("li.detail_trophy-awards")
        if trophies is not None:
            rec["award_text"] = trophies.text(strip=True) or None

        out.append(rec)
    return out


def extract_firm_profile(html: str) -> dict[str, Any]:
    """Doc §4.4 — firm website button + masthead phone.

    Doc-supplied selector ``a.profile-website-body[href]`` did NOT match
    on a sample firm (Prim & Mendheim) but the same anchor exists under
    a different class: ``a.webstats-website-click[href]`` (GTM event
    `pub_website_click`). Both selectors are tried in order so we
    survive A/B styling.
    """
    tree = HTMLParser(html)
    out: dict[str, Any] = {}

    website = tree.css_first(
        "a.webstats-website-click[href], a.profile-website-body[href]"
    )
    if website is not None:
        out["firm_website_url"] = website.attributes.get("href")
        rel = (website.attributes.get("rel") or "").lower()
        out["firm_website_is_sponsored"] = "sponsored" in rel

    # Masthead phone: try a tel: link near the top of the page.
    tel = tree.css_first("a[href^='tel:']")
    if tel is not None:
        out["firm_phone"] = (tel.attributes.get("href") or "").replace("tel:", "") or None

    return out


# ---- Driver ------------------------------------------------------------


def main() -> int:
    configure_logging()
    print("== Martindale reconnaissance ==\n")
    scraper = MartindaleScraper()

    try:
        # Step 1 — state index
        url = scraper.state_index_url()
        if not is_path_allowed(url):
            print(f"FATAL: {url} is on the forbidden-path list", file=sys.stderr)
            return 4
        gz = scraper.fetch_one(url, method="GET", bucket="index", filename="find_attorneys")
        html = _read_gz_text(gz)
        states = extract_states(html)
        _save_fixture("state_index", states)
        print(f"[1/4] State index: {len(states)} state links extracted (expect ~50).")
        if len(states) < 45:
            print(
                "      ! fewer than 45 states — page structure may have changed. "
                "First 3 anchors:"
            )
            for s in states[:3]:
                print(f"        {s}")

        # Step 2 — one state's city index (Alabama)
        alabama_url = scraper.state_url("alabama")
        gz = scraper.fetch_one(
            alabama_url, method="GET", bucket="state", filename="alabama"
        )
        html = _read_gz_text(gz)
        cities = extract_cities(html)
        _save_fixture("alabama_cities", cities)
        print(f"\n[2/4] Alabama cities: {len(cities)} city links extracted.")
        # Sample
        for c in cities[:3]:
            print(f"        {c['name']} -> {c['url']}")

        # Step 3 — one small city's attorney page (Abbeville)
        abbeville_url = scraper.city_url(city_slug="abbeville", state_slug="alabama")
        gz = scraper.fetch_one(
            abbeville_url, method="GET", bucket="city", filename="abbeville_alabama_p1"
        )
        html = _read_gz_text(gz)
        cards = extract_attorney_cards(html)
        _save_fixture("abbeville_alabama_attorneys", cards)
        print(f"\n[3/4] Abbeville, AL attorney cards: {len(cards)} extracted.")
        title_examples = sorted({c.get("title_raw") for c in cards if c.get("title_raw")})
        firms = sorted({c.get("firm_name_raw") for c in cards if c.get("firm_name_raw")})
        print(f"        Distinct firm names: {len(firms)}")
        for f in firms[:5]:
            print(f"          {f}")
        print(f"        Distinct title_raw values: {len(title_examples)}")
        for t in title_examples[:8]:
            print(f"          {t!r}")

        # Step 4 — one firm profile
        firm_urls = sorted(
            {
                c["firm_profile_url"]
                for c in cards
                if c.get("firm_profile_url")
            }
        )
        if not firm_urls:
            print("\n[4/4] No firm profile URLs found on this city's cards; skipping.")
        else:
            firm_url = firm_urls[0]
            print(f"\n[4/4] Firm profile: {firm_url}")
            gz = scraper.fetch_one(
                firm_url,
                method="GET",
                bucket="firm",
                filename="sample_firm",
            )
            html = _read_gz_text(gz)
            profile = extract_firm_profile(html)
            _save_fixture("sample_firm_profile", profile)
            for k, v in profile.items():
                print(f"        {k}: {v}")

    except ScrapeError as exc:
        msg = str(exc)
        if " 403 " in msg or " 503 " in msg or "-> 403" in msg or "-> 503" in msg:
            print(
                "\nFATAL: 403/503 from Martindale (likely Cloudflare). Stop and "
                "do not escalate tooling per docs/data_sources/martindale.md §3.3.\n"
                f"{msg}",
                file=sys.stderr,
            )
            return 3
        raise
    finally:
        scraper.close()

    print("\nDone. Fixtures saved under tests/fixtures/martindale/recon/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
