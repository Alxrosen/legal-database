"""Reconnaissance for the Martindale firm-profile enrichment pass.

Picks five subscriber firms of varied size / state from existing
FirmSourceRecord rows, fetches each profile, and runs a probe
against the selectors the user described in the spec:

  * ul.masthead-list > li.masthead-list__item — address / city-state /
    short-description lines (street, city/state, blurb)
  * ul#aopList — practice areas, one <li> per area
  * span.toggle-area__header-count — counts adjacent to "Areas of
    Practice" / "People" headings
  * div.truncate-text under h2 — "About our X office" / "Our Firm"
    description blocks
  * Office count + Year established — selectors TBD; the probe dumps
    text near common label words so we can find them

Outputs:
  * Raw pages saved via the BaseScraper to data/raw/...
  * One JSON inventory per profile under tests/fixtures/martindale/
    firm_profile_recon/{slug}.json with each selector's matched-or-not
    + a short value sample so we can see if shapes vary.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from selectolax.parser import HTMLParser  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from legal_sourcing.config import get_settings  # noqa: E402
from legal_sourcing.models import FirmSourceRecord  # noqa: E402
from legal_sourcing.scrapers.martindale import MartindaleScraper  # noqa: E402
from legal_sourcing.utils.logging import configure_logging, get_logger  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures" / "martindale" / "firm_profile_recon"
log = get_logger(__name__)


def _read_gz_text(path: Path) -> str:
    with gzip.open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def _slug_from_url(url: str) -> str:
    """Extract the firm slug from a Martindale profile URL.
    `.../organization/prim-mendheim-llc-24659303/dothan-alabama-...-f/`
    -> `prim-mendheim-llc-24659303`.
    """
    parts = [p for p in url.split("/") if p]
    for p in parts:
        if "-" in p and any(c.isdigit() for c in p):
            return p
    return "unknown"


def probe_profile(html: str) -> dict[str, Any]:
    """Run every selector the spec calls for and report what hit."""
    tree = HTMLParser(html)
    out: dict[str, Any] = {}

    # Masthead list — address + city/state + short description
    masthead = tree.css("ul.masthead-list li.masthead-list__item")
    out["masthead_list_items_count"] = len(masthead)
    out["masthead_list_items"] = []
    for li in masthead:
        cls = li.attributes.get("class") or ""
        out["masthead_list_items"].append(
            {
                "class": cls,
                "text": li.text(strip=True),
                "is_bold": "masthead-list__item--bold" in cls,
            }
        )

    # Practice areas — ul#aopList
    aop = tree.css_first("ul#aopList")
    if aop is not None:
        items = [li.text(strip=True) for li in aop.css("li") if li.text(strip=True)]
        out["aop_list_count"] = len(items)
        out["aop_list_items"] = items
    else:
        out["aop_list_count"] = None
        out["aop_list_items"] = None

    # toggle-area header counts — these sit adjacent to h2 elements
    # like "Areas of Practice" / "People". Map every header-count we
    # find to its preceding h2 text.
    out["toggle_area_header_counts"] = []
    for el in tree.css("span.toggle-area__header-count"):
        # Walk siblings backwards for the nearest h2.
        h2_text: str | None = None
        cur = el.parent
        if cur is not None:
            h2 = cur.css_first("h2")
            if h2 is not None:
                h2_text = h2.text(strip=True)
        out["toggle_area_header_counts"].append(
            {"header": h2_text, "count_text": el.text(strip=True)}
        )

    # Description blocks — div.truncate-text under h2.
    out["truncate_text_blocks"] = []
    for tt in tree.css("div.truncate-text"):
        # Find the nearest h2 in the same container.
        cur = tt.parent
        h2_text = None
        if cur is not None:
            h2 = cur.css_first("h2")
            if h2 is not None:
                h2_text = h2.text(strip=True)
        out["truncate_text_blocks"].append(
            {
                "header": h2_text,
                "text_sample": tt.text(strip=True)[:300],
                "text_length": len(tt.text(strip=True)),
            }
        )

    # Office count / Year established — try definition-list shapes
    # and a regex over the whole page for "Year Established: NNNN"
    # and "Office Size: N".
    out["year_established_regex"] = None
    out["office_count_regex"] = None
    body_text = tree.body.text(strip=False) if tree.body else html
    m = re.search(r"Year\s+Established[^A-Za-z0-9]+([12][0-9]{3})", body_text, flags=re.I)
    if m:
        out["year_established_regex"] = int(m.group(1))
    m = re.search(
        r"(?:Office\s+Size|Office\s+Count)[^A-Za-z0-9]+(\d+)", body_text, flags=re.I
    )
    if m:
        out["office_count_regex"] = int(m.group(1))

    # Common label / value <dl> or <table> selectors to surface anything
    # near the page that mentions Office or Year.
    out["near_year_label_text"] = []
    for el in tree.css("*"):
        t = el.text(strip=True) if el.text(strip=True) else ""
        if "Year Established" in t and len(t) < 80:
            out["near_year_label_text"].append({"tag": el.tag, "text": t})
    # Cap the size of the spam — we just need the first few.
    out["near_year_label_text"] = out["near_year_label_text"][:5]

    # Subscriber / preeminent / awards / ratings — surface for later.
    out["preeminent_present"] = "Martindale-Hubbell" in body_text and "Preeminent" in body_text
    out["peer_reviews_present"] = "Peer Reviews" in body_text

    return out


def main() -> int:
    configure_logging()
    settings = get_settings()
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    # Pick 5 subscriber Martindale firms of varied size / state.
    engine = create_engine(settings.db_url)
    with Session(engine) as s:
        rows = s.scalars(
            select(FirmSourceRecord).where(FirmSourceRecord.source == "martindale")
        ).all()

    # Filter: must have firm_profile_url. Then pick a mix.
    with_profile = [
        r for r in rows
        if (r.additional_data or {}).get("firm_profile_url")
    ]
    print(f"Martindale rows with firm_profile_url: {len(with_profile)}/{len(rows)}")

    # Pick a mix by attorney_count: 1, 2-3, 4-7, 8+, and one from AZ.
    candidates: list[FirmSourceRecord] = []
    by_size = {
        "1 attorney": [r for r in with_profile if (r.attorney_count or 0) == 1],
        "2-3 attorneys": [r for r in with_profile if 2 <= (r.attorney_count or 0) <= 3],
        "4-7 attorneys": [r for r in with_profile if 4 <= (r.attorney_count or 0) <= 7],
        "8+ attorneys": [r for r in with_profile if (r.attorney_count or 0) >= 8],
    }
    for label, group in by_size.items():
        if group:
            candidates.append(group[0])
            print(f"  picked {label}: {group[0].name_raw}")

    # Add an Arizona firm for state diversity if we don't already have one.
    if not any((c.additional_data or {}).get("state_slug") == "arizona" for c in candidates):
        az = next(
            (r for r in with_profile if (r.additional_data or {}).get("state_slug") == "arizona"),
            None,
        )
        if az is not None:
            candidates.append(az)
            print(f"  picked Arizona: {az.name_raw}")

    if not candidates:
        print("No candidates found.", file=sys.stderr)
        return 1

    inventory: list[dict[str, Any]] = []
    with MartindaleScraper() as scraper:
        for r in candidates:
            url = (r.additional_data or {}).get("firm_profile_url")
            slug = _slug_from_url(url)
            print(f"\nFetching: {r.name_raw}  [{slug}]")
            path = scraper.fetch_one(
                url, method="GET", bucket="firm_profile_recon", filename=slug
            )
            html = _read_gz_text(path)
            probe = probe_profile(html)
            probe["_firm_name"] = r.name_raw
            probe["_firm_profile_url"] = url
            probe["_slug"] = slug
            probe["_attorney_count_srp"] = r.attorney_count
            probe["_state_slug"] = (r.additional_data or {}).get("state_slug")
            inventory.append(probe)
            # Per-firm fixture.
            (FIXTURES_DIR / f"{slug}_probe.json").write_text(
                json.dumps(probe, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    # Cross-firm summary — does every probe find the same selectors?
    print("\n== Selector-availability summary across the 5 firms ==")
    print(f"{'firm':40s} {'masthead':>10s} {'aop':>5s} {'header_counts':>14s} {'truncates':>10s} {'year_re':>8s}")
    for inv in inventory:
        print(
            f"{inv['_firm_name'][:40]:40s} "
            f"{inv['masthead_list_items_count']:>10d} "
            f"{(inv['aop_list_count'] or 0):>5d} "
            f"{len(inv['toggle_area_header_counts']):>14d} "
            f"{len(inv['truncate_text_blocks']):>10d} "
            f"{str(inv['year_established_regex']):>8s}"
        )
    print(f"\nPer-firm fixtures: {FIXTURES_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
