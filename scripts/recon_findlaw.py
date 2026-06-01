"""FindLaw reconnaissance — 8 phases per docs/data_sources/findlaw_reference.md.

Phases:
  1. Robots + sitemaps
  2. Practice-area index (a-z)
  3. State index
  4. Practice-area state landing
  5. Practice-area city listing (small) — Alabaster, AL
  6. Practice-area city listing (large) + pagination — Birmingham, AL
  7. Cloudflare probe (passive: all responses watched throughout)
  8. One attorney profile (depth check)

Failure modes:
  * FindLawCloudflareChallenge -> abort the run with a clear message.
  * Per-phase structural surprise -> warn and continue; the recon script
    is supposed to surface deltas, not the parser.

Outputs:
  * Raw gzipped responses under data/raw/findlaw/{date}/recon/...
  * Pretty-printed extractor output to tests/fixtures/findlaw/recon/
  * Practice-area list to data/reference/findlaw_practice_areas.csv
  * Compact shape summary printed for each phase
"""

from __future__ import annotations

import csv
import gzip
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from selectolax.parser import HTMLParser  # noqa: E402

from legal_sourcing.scrapers.base import ScrapeError  # noqa: E402
from legal_sourcing.scrapers.findlaw import (  # noqa: E402
    FindLawCloudflareChallenge,
    FindLawScraper,
)
from legal_sourcing.utils.logging import configure_logging, get_logger  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures" / "findlaw" / "recon"
REFERENCE_DIR = ROOT / "data" / "reference"
log = get_logger(__name__)


def _save_fixture(name: str, data: Any) -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURES_DIR / f"{name}.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def _read_gz_text(path: Path) -> str:
    with gzip.open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def _save_text(name: str, text: str, *, ext: str = "txt") -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURES_DIR / f"{name}.{ext}"
    out.write_text(text, encoding="utf-8")
    return out


# ---- Phase 1: robots + sitemaps ---------------------------------------


def phase_1_robots_and_sitemaps(scraper: FindLawScraper) -> dict[str, Any]:
    print("\n[1/8] Robots + sitemaps")
    out: dict[str, Any] = {}
    # robots.txt is fetched separately because it doesn't go through
    # the bucket/filename rig.
    robots_url = f"{scraper.BASE_URL}/robots.txt"
    path = scraper.fetch_one(robots_url, bucket="robots", filename="robots")
    robots_text = _read_gz_text(path)
    _save_text("robots", robots_text)
    out["robots_url"] = robots_url
    out["robots_first_400_chars"] = robots_text[:400]

    # Extract sitemap URLs from robots.txt
    sitemap_urls = re.findall(
        r"^\s*Sitemap:\s*(\S+)\s*$", robots_text, flags=re.IGNORECASE | re.MULTILINE
    )
    out["sitemap_urls"] = sitemap_urls
    print(f"  robots.txt: {len(robots_text)} bytes; {len(sitemap_urls)} sitemap URLs")

    out["sitemaps"] = []
    for url in sitemap_urls:
        try:
            sm_path = scraper.fetch_one(
                url,
                bucket="sitemaps",
                filename=urlparse(url).path.rsplit("/", 1)[-1].replace(".xml", "") or "sitemap",
            )
        except ScrapeError as exc:
            print(f"  sitemap fetch failed: {url} -> {exc}")
            out["sitemaps"].append({"url": url, "error": str(exc)})
            continue
        sm_text = _read_gz_text(sm_path)
        # Count <loc> entries and the kind of URL inside.
        loc_matches = re.findall(r"<loc>([^<]+)</loc>", sm_text)
        sample = loc_matches[:5]
        out["sitemaps"].append(
            {
                "url": url,
                "bytes": len(sm_text),
                "loc_count": len(loc_matches),
                "sample_urls": sample,
            }
        )
        print(
            f"  sitemap {urlparse(url).path}: {len(loc_matches)} URLs, "
            f"first sample: {sample[0] if sample else '(empty)'}"
        )
    _save_fixture("phase1_robots_and_sitemaps", out)
    return out


# ---- Phase 2: practice-area index --------------------------------------


def phase_2_practice_area_index(scraper: FindLawScraper) -> list[dict[str, str]]:
    print("\n[2/8] Practice-area A-Z index")
    url = scraper.practice_area_index_url()
    path = scraper.fetch_one(url, bucket="index", filename="legal_issues")
    html = _read_gz_text(path)
    tree = HTMLParser(html)
    anchors = tree.css("a.fl-list-item-link")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in anchors:
        href = a.attributes.get("href") or ""
        name = a.text(strip=True)
        if not href or not name:
            continue
        # Practice-area slug is the first path segment after the host.
        p = urlparse(href).path.strip("/")
        if not p:
            continue
        slug = p.split("/", 1)[0]
        if slug in seen:
            continue
        seen.add(slug)
        out.append({"slug": slug, "name": name, "url": href})
    _save_fixture("phase2_practice_areas", out)
    print(f"  practice areas extracted: {len(out)}")
    if out:
        print(f"  first 3: {[(p['slug'], p['name']) for p in out[:3]]}")

    # Also save a CSV the user can review and pick from.
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = REFERENCE_DIR / "findlaw_practice_areas.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["slug", "name", "url"])
        for p in out:
            w.writerow([p["slug"], p["name"], p["url"]])
    print(f"  CSV saved: {csv_path}")
    return out


# ---- Phase 3: state index ---------------------------------------------


def phase_3_state_index(scraper: FindLawScraper) -> list[dict[str, str]]:
    print("\n[3/8] State index")
    url = scraper.root_url()
    path = scraper.fetch_one(url, bucket="index", filename="root")
    html = _read_gz_text(path)
    tree = HTMLParser(html)
    anchors = tree.css("ul#map-module-state-list a.map-module-state-list-link")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in anchors:
        href = a.attributes.get("href") or ""
        name = a.text(strip=True)
        if not href or not name:
            continue
        slug = urlparse(href).path.strip("/").split("/", 1)[0]
        if slug in seen:
            continue
        seen.add(slug)
        out.append({"slug": slug, "name": name, "url": href})
    _save_fixture("phase3_states", out)
    print(f"  state links: {len(out)} (expect ~50 + DC)")
    return out


# ---- Phase 4: practice-area state landing -----------------------------


def phase_4_practice_area_state_landing(
    scraper: FindLawScraper,
    *,
    practice_area_slug: str = "motor-vehicle-accidents-plaintiff",
    state_slug: str = "alabama",
) -> dict[str, Any]:
    print(
        f"\n[4/8] Practice-area state landing: /{practice_area_slug}/{state_slug}/"
    )
    url = scraper.practice_area_state_url(
        practice_area_slug=practice_area_slug, state_slug=state_slug
    )
    path = scraper.fetch_one(
        url, bucket="practice_state", filename=f"{practice_area_slug}__{state_slug}"
    )
    html = _read_gz_text(path)
    tree = HTMLParser(html)
    out: dict[str, Any] = {"url": url}

    # Heuristic: city links inside the page that point into the
    # /{practice_area}/{state}/{city}/ path.
    candidates = tree.css("a[href]")
    city_links: list[dict[str, str]] = []
    seen: set[str] = set()
    needed_prefix = f"/{practice_area_slug}/{state_slug}/"
    for a in candidates:
        href = a.attributes.get("href") or ""
        if not href:
            continue
        p = urlparse(href).path
        if not p.startswith(needed_prefix):
            continue
        # Skip the state landing itself.
        remainder = p[len(needed_prefix):].strip("/")
        if not remainder or "/" in remainder:
            continue
        if href in seen:
            continue
        seen.add(href)
        city_links.append({"name": a.text(strip=True) or remainder, "url": href})
    out["city_links_count"] = len(city_links)
    out["city_links_sample"] = city_links[:5]

    # Note if there's a pagination footer on this page.
    out["has_pagination_nav"] = bool(tree.css_first('nav[aria-label="Pagination"]'))

    _save_fixture("phase4_practice_area_state_landing", out)
    print(f"  city links: {len(city_links)}; pagination: {out['has_pagination_nav']}")
    if city_links:
        print(f"  first 3: {city_links[:3]}")
    return out


# ---- Phase 5: practice-area city listing (small) ---------------------


def _extract_card(card_node) -> dict[str, Any]:
    """Best-effort fl-serp-card.organic extraction."""
    rec: dict[str, Any] = {}
    title_a = card_node.css_first('a.fl-serp-card-title, a[data-testid="serp-card-title-link"]')
    if title_a is not None:
        rec["title_url"] = title_a.attributes.get("href")
        rec["title_text"] = title_a.text(strip=True) or None
    txt = card_node.css_first('div.fl-serp-card-text, [data-testid="serp-card-text"]')
    if txt is not None:
        rec["card_text"] = txt.text(strip=True) or None
    loc = card_node.css_first("div.fl-serp-card-location > span, div.fl-serp-card-location")
    if loc is not None:
        rec["location_text"] = loc.text(strip=True) or None
    site = card_node.css_first('a[data-testid="website-button-link"]')
    if site is not None:
        rec["website_url"] = site.attributes.get("href")
        rec["website_rel"] = site.attributes.get("rel")
    phone = card_node.css_first('a[data-testid="phone-button-link"], a[href^="tel:"]')
    if phone is not None:
        h = phone.attributes.get("href") or ""
        rec["phone"] = h.replace("tel:", "") or None
    rec["data_testid"] = card_node.attributes.get("data-testid")
    return rec


def phase_5_city_small(scraper: FindLawScraper) -> dict[str, Any]:
    print("\n[5/8] Practice-area city listing (small): Alabaster, AL — bare URL")
    practice = "motor-vehicle-accidents-plaintiff"
    state = "alabama"
    city = "alabaster"
    url_bare = scraper.practice_area_city_url(
        practice_area_slug=practice, state_slug=state, city_slug=city
    )
    path = scraper.fetch_one(
        url_bare, bucket="city_small", filename=f"{city}_{state}_bare"
    )
    html = _read_gz_text(path)
    tree = HTMLParser(html)
    cards = tree.css("div.fl-serp-card.organic, .fl-serp-card.organic")
    out: dict[str, Any] = {"url": url_bare, "card_count": len(cards)}
    extracted = [_extract_card(c) for c in cards]
    out["cards"] = extracted
    _save_fixture("phase5_alabaster_bare", extracted)

    # Now refetch WITH the full observed query string and compare.
    qs = "keyword=Car+Accident&location=Alabaster%2C+AL&stype=BY_ADDR_OR_ZIP"
    url_full = scraper.practice_area_city_url(
        practice_area_slug=practice, state_slug=state, city_slug=city, extra_params=qs
    )
    path2 = scraper.fetch_one(
        url_full, bucket="city_small", filename=f"{city}_{state}_full"
    )
    html2 = _read_gz_text(path2)
    tree2 = HTMLParser(html2)
    cards2 = tree2.css("div.fl-serp-card.organic, .fl-serp-card.organic")
    out["url_full"] = url_full
    out["card_count_full"] = len(cards2)
    out["cards_full"] = [_extract_card(c) for c in cards2]
    _save_fixture("phase5_alabaster_full", out["cards_full"])

    print(
        f"  bare URL: {len(cards)} cards   |   full query string: {len(cards2)} cards"
    )
    if extracted:
        print(f"  first card (bare): {extracted[0]}")
    return out


# ---- Phase 6: pagination discovery -----------------------------------


def phase_6_pagination(scraper: FindLawScraper) -> dict[str, Any]:
    print("\n[6/8] Pagination discovery: Birmingham, AL motor-vehicle-plaintiff")
    practice = "motor-vehicle-accidents-plaintiff"
    state = "alabama"
    city = "birmingham"

    url1 = scraper.practice_area_city_url(
        practice_area_slug=practice, state_slug=state, city_slug=city
    )
    p1 = scraper.fetch_one(
        url1, bucket="city_pagination", filename=f"{city}_{state}_p01"
    )
    html1 = _read_gz_text(p1)
    tree1 = HTMLParser(html1)
    cards1 = tree1.css("div.fl-serp-card.organic, .fl-serp-card.organic")

    nav = tree1.css_first('nav[aria-label="Pagination"]')
    next_a = tree1.css_first(
        'a.fl-pagination-button[rel="next"], a[data-testid="fl-pagination-button-next"]'
    )
    numbered_pages: list[int] = []
    if nav is not None:
        for a in nav.css("a, li"):
            t = a.text(strip=True) or ""
            if t.isdigit():
                numbered_pages.append(int(t))
    last_page = max(numbered_pages) if numbered_pages else None

    # Try to find a "Results X to Y of N" text by scanning common
    # locations and full body for the regex.
    body_text = tree1.body.text(strip=False) if tree1.body else html1
    m = re.search(r"Results?\s+\d[\d,]*\s+to\s+\d[\d,]*\s+of\s+([\d,]+)", body_text)
    results_total = None
    if m:
        results_total = int(m.group(1).replace(",", ""))

    out: dict[str, Any] = {
        "url": url1,
        "page1_card_count": len(cards1),
        "has_pagination_nav": nav is not None,
        "has_next_link": next_a is not None,
        "numbered_pages_seen": numbered_pages[:20],
        "last_page": last_page,
        "results_total_via_text_regex": results_total,
    }
    print(
        f"  page1: {len(cards1)} cards | nav={nav is not None} "
        f"next={next_a is not None} last_page={last_page} "
        f"results_total~{results_total}"
    )

    # Fetch page 2 to confirm pattern + cards differ.
    if last_page and last_page >= 2:
        url2 = scraper.practice_area_city_url(
            practice_area_slug=practice, state_slug=state, city_slug=city, page=2
        )
        p2 = scraper.fetch_one(
            url2, bucket="city_pagination", filename=f"{city}_{state}_p02"
        )
        html2 = _read_gz_text(p2)
        tree2 = HTMLParser(html2)
        cards2 = tree2.css("div.fl-serp-card.organic, .fl-serp-card.organic")
        # Sample first URL on each page; should differ.
        first_a_1 = tree1.css_first("a.fl-serp-card-title")
        first_a_2 = tree2.css_first("a.fl-serp-card-title")
        out["page2_card_count"] = len(cards2)
        out["page1_first_card_url"] = first_a_1.attributes.get("href") if first_a_1 else None
        out["page2_first_card_url"] = first_a_2.attributes.get("href") if first_a_2 else None
        print(
            f"  page2: {len(cards2)} cards | first url differs from page1: "
            f"{out['page1_first_card_url'] != out['page2_first_card_url']}"
        )

    _save_fixture("phase6_pagination", out)
    return out


# ---- Phase 7: Cloudflare probe (summary across phases) ----------------


def phase_7_cloudflare_summary() -> None:
    print("\n[7/8] Cloudflare probe — no challenge raised across phases 1-6.")
    print("  (Any FindLawCloudflareChallenge would have aborted before this point.)")


# ---- Phase 8: one attorney profile ------------------------------------


def phase_8_one_profile(scraper: FindLawScraper, phase5: dict[str, Any]) -> dict[str, Any]:
    print("\n[8/8] One attorney profile (depth check)")
    # Use the first card with a title_url from Alabaster.
    cards = (phase5.get("cards") or []) + (phase5.get("cards_full") or [])
    chosen = None
    for c in cards:
        if c.get("title_url"):
            chosen = c
            break
    if chosen is None:
        print("  no card URL captured in phase 5; skipping profile fetch")
        return {"skipped": True}

    profile_url = chosen["title_url"]
    try:
        path = scraper.fetch_one(profile_url, bucket="profile", filename="sample_profile")
    except ScrapeError as exc:
        print(f"  profile fetch failed: {exc}")
        return {"error": str(exc), "url": profile_url}

    html = _read_gz_text(path)
    tree = HTMLParser(html)
    # Inventory likely-useful field locations to flag for the parser.
    out: dict[str, Any] = {
        "url": profile_url,
        "bytes": len(html),
        "title_tag": (tree.css_first("title").text(strip=True) if tree.css_first("title") else None),
        "h1_count": len(tree.css("h1")),
        "h1_first": (tree.css("h1")[0].text(strip=True) if tree.css("h1") else None),
        "has_practice_area_list": bool(
            tree.css_first('[data-testid="practice-areas"], .practice-area, .fl-practice-area')
        ),
        "has_bar_admissions": bool(
            tree.css_first('[data-testid="bar-admissions"], .bar-admissions')
        ),
        "tel_count": len(tree.css("a[href^='tel:']")),
        "website_button_count": len(tree.css('a[data-testid="website-button-link"]')),
    }
    _save_fixture("phase8_profile_inventory", out)
    print(f"  profile: {out}")
    return out


# ---- Driver ------------------------------------------------------------


def main() -> int:
    configure_logging()
    print("== FindLaw reconnaissance ==")

    scraper = FindLawScraper()

    # Dump headers so the operator can verify against DevTools.
    print("\nOutgoing request headers:")
    for k, v in sorted(dict(scraper._client.headers).items()):
        print(f"  {k}: {v}")

    try:
        p1 = phase_1_robots_and_sitemaps(scraper)
        p2 = phase_2_practice_area_index(scraper)
        p3 = phase_3_state_index(scraper)
        p4 = phase_4_practice_area_state_landing(scraper)
        p5 = phase_5_city_small(scraper)
        p6 = phase_6_pagination(scraper)
        phase_7_cloudflare_summary()
        p8 = phase_8_one_profile(scraper, p5)
    except FindLawCloudflareChallenge as exc:
        print(f"\nFATAL CLOUDFLARE CHALLENGE: {exc}", file=sys.stderr)
        return 4
    except ScrapeError as exc:
        print(f"\nFATAL ScrapeError: {exc}", file=sys.stderr)
        return 3
    finally:
        scraper.close()

    print("\nDone. Fixtures saved under tests/fixtures/findlaw/recon/.")
    print("Practice-area CSV: data/reference/findlaw_practice_areas.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
