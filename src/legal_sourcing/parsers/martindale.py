"""Martindale-Hubbell parsers — city attorney pages and firm profiles.

The city parser emits firm-shaped dicts (one per attorney card) matching
the same shape AZ Bar produces. The pipeline aggregates across attorneys
at the same firm.

Three card shapes observed (only two documented in
docs/data_sources/martindale.md):

  1. **Subscriber** (~22% in the Abbeville sample): has a linked firm
     anchor (`a.detail_position--office-link`). All `detail_*` fields
     populated.
  2. **Solo** (firm name = attorney name, no firm anchor): the
     `detail_position` text is freeform like `"Solo Practitioner"`.
     Treated as `__solo__` at aggregation time.
  3. **Non-subscriber affiliated** (new finding 2026-05-29): no firm
     anchor, but the position text follows the pattern `"Title at
     Firm Name"` (e.g. `"Member at DaGian Law Offices, LLP"`). We
     split on `" at "` to recover both title and firm name.

`source_firm_id_martindale` (from `data-gtm-tracking` JSON on the firm
anchor) and `firm_website_is_sponsored` from the firm-profile page
land in `additional_data` — they're informational, not promoted to
typed columns.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from legal_sourcing.parsers.base import BaseParser

_ATTORNEY_ID_RE = re.compile(r"-(\d+)/?$")
# US-state postal codes for the location_text city/state parse.
_US_STATE_RE = re.compile(r"^([\w\s.\-']+?),\s*([A-Z]{2})$")


def _parse_gtm(attr_value: str) -> dict[str, Any] | None:
    if not attr_value:
        return None
    try:
        return json.loads(attr_value)
    except json.JSONDecodeError:
        return None


def _split_title_at_firm(text: str) -> tuple[str | None, str | None]:
    """Split a `"X at Y"` position string into (title, firm_name).

    Returns (None, None) for empty input. For inputs without ` at `,
    returns (text, None) — the whole text is the title.

    Also handles selectolax's `text(strip=True)` artifact: when the
    DOM is `"Managing Partner at " <a>Firm Name</a>` and we slice off
    the firm anchor, the residual title text is `"Managing Partner at"`
    (no trailing whitespace, no firm). Strip that trailing `" at"` so
    we don't store it as part of the title.
    """
    if not text:
        return None, None
    s = text.strip()
    # Standard "Title at Firm Name" case.
    parts = re.split(r"\s+at\s+", s, maxsplit=1)
    if len(parts) == 2:
        title = parts[0].strip().rstrip(",")
        firm = parts[1].strip()
        return (title or None), (firm or None)
    # Trailing "at" with no firm (subscriber-card slicing artifact).
    if s.endswith(" at"):
        s = s[:-3].rstrip(",").strip()
    return (s or None), None


def _parse_location_text(text: str | None) -> dict[str, Any]:
    """Best-effort split of `"City, ST"` into city + state."""
    if not text:
        return {}
    s = text.strip()
    m = _US_STATE_RE.match(s)
    if m:
        return {"city_raw": m.group(1).strip(), "state_raw": m.group(2)}
    # Fallback: city only.
    return {"city_raw": s}


def _attorney_card_to_firm_dict(
    card_html, *, city_slug: str, state_slug: str, page_url: str
) -> dict[str, Any] | None:
    """Convert one attorney card (selectolax Node) to a firm-shaped dict.

    Returns None if the card lacks an attorney name (we can't use it).
    """
    rec: dict[str, Any] = {}

    title_a = card_html.css_first("li.detail_title > a")
    attorney_name: str | None = None
    attorney_profile_url: str | None = None
    source_attorney_id: str | None = None
    if title_a is not None:
        attorney_profile_url = title_a.attributes.get("href") or None
        h3 = title_a.css_first("h3")
        if h3 is not None:
            attorney_name = h3.text(strip=True) or None
        if attorney_profile_url:
            m = _ATTORNEY_ID_RE.search(urlparse(attorney_profile_url).path)
            if m:
                source_attorney_id = m.group(1)

    if not attorney_name:
        return None  # un-usable card

    pos = card_html.css_first("li.detail_position")
    title_raw: str | None = None
    firm_name_raw: str | None = None
    firm_profile_url: str | None = None
    source_firm_id_martindale: str | None = None
    if pos is not None:
        firm_a = pos.css_first("a.detail_position--office-link")
        if firm_a is not None:
            # Subscriber card — full structure.
            pre = (pos.text(strip=True) or "").split(firm_a.text(strip=True))[0]
            title_raw, _ = _split_title_at_firm(pre)
            firm_name_raw = firm_a.text(strip=True) or None
            firm_profile_url = firm_a.attributes.get("href") or None
            gtm = _parse_gtm(firm_a.attributes.get("data-gtm-tracking") or "")
            if gtm:
                v = gtm.get("firm_id")
                source_firm_id_martindale = str(v) if v is not None else None
        else:
            # Non-subscriber: position is freeform. Try "X at Y" split.
            t, f = _split_title_at_firm(pos.text(strip=True) or "")
            title_raw = t
            firm_name_raw = f  # may be None (solo case)

    # Contact phone (tel: anchor) and location text.
    tel = card_html.css_first("a[href^='tel:']")
    phone_raw = None
    if tel is not None:
        phone_raw = (tel.attributes.get("href") or "").replace("tel:", "") or None

    loc = card_html.css_first("li.detail_location")
    location_text = loc.text(strip=True) if loc is not None else None
    loc_parts = _parse_location_text(location_text)

    # Awards / bio snippet (optional).
    trophies = card_html.css_first("li.detail_trophy-awards")
    award_text = trophies.text(strip=True) if trophies is not None else None
    award_count: int | None = None
    if award_text:
        m = re.match(r"(\d+)", award_text)
        award_count = int(m.group(1)) if m else None

    bio = card_html.css_first("li.detail_bio")
    bio_text = bio.text(strip=True) if bio is not None else None

    # Build the contact entry.
    contact: dict[str, Any] = {
        "name_raw": attorney_name,
        "title": title_raw,
        "phone_raw": phone_raw,
        "source_attorney_id": source_attorney_id,
        "source_attorney_url": attorney_profile_url,
    }
    if bio_text:
        contact["bio_snippet"] = bio_text
    if award_count is not None:
        contact["award_count"] = award_count

    # Build the office entry (only city/state — no street on the card).
    office: dict[str, Any] | None = None
    if loc_parts:
        office = {
            "label": None,
            "is_primary": True,
            "country_raw": "US",
            **loc_parts,
        }
        if phone_raw:
            office["phone_raw"] = phone_raw

    additional: dict[str, Any] = {
        "card_shape": "subscriber" if source_firm_id_martindale else ("solo" if firm_name_raw is None else "non_subscriber_at_pattern"),
    }
    if source_firm_id_martindale:
        additional["source_firm_id_martindale"] = source_firm_id_martindale
    if firm_profile_url:
        additional["firm_profile_url"] = firm_profile_url
    additional["city_slug"] = city_slug
    additional["state_slug"] = state_slug

    return {
        "name_raw": firm_name_raw,
        "deactivation_status": None,
        "website_raw": None,  # populated post-hoc from firm profile
        "phone_raw": phone_raw,
        "year_founded": None,
        "attorney_count": None,
        "source_last_updated_at": None,
        "contacts": [contact],
        "offices": [office] if office else [],
        "practice_areas_raw": [],  # Martindale cards don't list these
        "additional_data": additional,
    }


class MartindaleCityParser(BaseParser):
    """Parse one city attorney-results page into per-attorney dicts."""

    SOURCE_NAME = "martindale"

    def parse_bytes(
        self, payload: bytes, *, source_url: str
    ) -> list[dict[str, Any]]:
        html = payload.decode("utf-8", errors="replace")
        tree = HTMLParser(html)
        # Derive city + state slugs from the URL so each emitted dict
        # carries provenance independent of the page DOM.
        path = urlparse(source_url).path.strip("/").split("/")
        # Expected shape: ["all-lawyers", "<city>", "<state>"]
        city_slug = path[1] if len(path) >= 2 else ""
        state_slug = path[2] if len(path) >= 3 else ""

        out: list[dict[str, Any]] = []
        for card in tree.css(".card.card--attorney"):
            rec = _attorney_card_to_firm_dict(
                card, city_slug=city_slug, state_slug=state_slug, page_url=source_url
            )
            if rec is not None:
                out.append(rec)
        return out


def extract_page_meta(html: str) -> dict[str, Any]:
    """Extract pagination + result-count metadata from a city listing page.

    Returns a dict with:
      * `results_total`: int | None — declared total result count from
        the `.results__total` span (e.g. "(6,994)" -> 6994). NOT
        load-bearing — directory sites often inflate this; use it only
        as a sanity baseline.
      * `last_page`: int | None — total page count. Prefer
        `input.goToPage[data-max]`; fall back to the largest
        `data-page` integer on any `<a>` in the pagination block.
      * `has_next`: bool — True if `a.arrow[rel="next"]` exists AND
        does NOT carry the `unavailable` class (last page disables
        the next arrow).
      * `card_count`: int — number of attorney cards on this page,
        for the per-page accounting that drives the count-mismatch
        warning.
    """
    tree = HTMLParser(html)

    results_total: int | None = None
    total_span = tree.css_first(".results__total")
    if total_span is not None:
        t = total_span.text(strip=True) or ""
        m = re.search(r"[\d,]+", t)
        if m:
            try:
                results_total = int(m.group(0).replace(",", ""))
            except ValueError:
                results_total = None

    last_page: int | None = None
    goto = tree.css_first("input.goToPage[data-max]")
    if goto is not None:
        dm = (goto.attributes.get("data-max") or "").strip()
        if dm.isdigit():
            last_page = int(dm)
    if last_page is None:
        # Fallback: scan numbered <a data-page=...> inside any pagination ul.
        for a in tree.css("ul.pagination a[data-page]"):
            dp = (a.attributes.get("data-page") or "").strip()
            if dp.isdigit():
                last_page = max(last_page or 0, int(dp))

    next_a = tree.css_first('a.arrow[rel="next"]')
    has_next = False
    if next_a is not None:
        cls = (next_a.attributes.get("class") or "").lower()
        # The previous-page-of-page-1 case shows `class="arrow unavailable"`
        # on the prev arrow; the next arrow uses the same convention on
        # the last page.
        if "unavailable" not in cls:
            has_next = True

    card_count = len(tree.css(".card.card--attorney"))

    return {
        "results_total": results_total,
        "last_page": last_page,
        "has_next": has_next,
        "card_count": card_count,
    }


def parse_firm_profile(payload: bytes) -> dict[str, Any]:
    """Extract firm-level fields from a profile page. Returned as a
    free dict; the pipeline merges into the aggregated FirmSourceRecord
    via `firm_profile_url`.
    """
    html = payload.decode("utf-8", errors="replace")
    tree = HTMLParser(html)
    out: dict[str, Any] = {}

    website = tree.css_first(
        "a.webstats-website-click[href], a.profile-website-body[href]"
    )
    if website is not None:
        out["firm_website_url"] = website.attributes.get("href")
        rel = (website.attributes.get("rel") or "").lower()
        out["firm_website_is_sponsored"] = "sponsored" in rel

    tel = tree.css_first("a[href^='tel:']")
    if tel is not None:
        out["firm_phone"] = (tel.attributes.get("href") or "").replace("tel:", "") or None

    return out
