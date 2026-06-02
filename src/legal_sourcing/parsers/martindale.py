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

from legal_sourcing.normalize.url import safe_urlparse, strip_self_domain
from legal_sourcing.parsers.base import BaseParser

# Never record Martindale's own domain as a firm website (would collapse
# to a shared key and cause false website matches in resolution).
_MARTINDALE_OWN_DOMAINS = ("martindale.com",)

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
            _parsed = safe_urlparse(attorney_profile_url)
            if _parsed is not None:
                m = _ATTORNEY_ID_RE.search(_parsed.path)
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

    # Subscriber detection from data-gtm-tracking on the SRP card.
    # The firm anchor on subscriber cards carries
    # `"profile_type":"Subscriber"`; non-subscriber cards have no such
    # anchor at all (matched by the firm_a is None branch above). We
    # also look at the attorney title anchor's gtm payload as a
    # secondary signal — some cards have "profile_type":"Subscriber"
    # on the attorney side too.
    is_subscriber = False
    if pos is not None:
        firm_a_for_gtm = pos.css_first("a.detail_position--office-link")
        if firm_a_for_gtm is not None:
            gtm_firm = _parse_gtm(firm_a_for_gtm.attributes.get("data-gtm-tracking") or "")
            if gtm_firm and gtm_firm.get("profile_type") == "Subscriber":
                is_subscriber = True
    title_a_for_gtm = card_html.css_first("li.detail_title > a")
    if title_a_for_gtm is not None and not is_subscriber:
        gtm_title = _parse_gtm(title_a_for_gtm.attributes.get("data-gtm-tracking") or "")
        if gtm_title and gtm_title.get("profile_type") == "Subscriber":
            is_subscriber = True

    additional: dict[str, Any] = {
        "card_shape": "subscriber"
        if source_firm_id_martindale
        else ("solo" if firm_name_raw is None else "non_subscriber_at_pattern"),
        "is_subscriber": is_subscriber,
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

    def parse_bytes(self, payload: bytes, *, source_url: str) -> list[dict[str, Any]]:
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

    website = tree.css_first("a.webstats-website-click[href], a.profile-website-body[href]")
    if website is not None:
        out["firm_website_url"] = strip_self_domain(
            website.attributes.get("href"), _MARTINDALE_OWN_DOMAINS
        )
        rel = (website.attributes.get("rel") or "").lower()
        out["firm_website_is_sponsored"] = "sponsored" in rel

    tel = tree.css_first("a[href^='tel:']")
    if tel is not None:
        out["firm_phone"] = (tel.attributes.get("href") or "").replace("tel:", "") or None

    return out


# ---------------------------------------------------------------------------
# Full firm-profile parser — enrichment pass for subscriber firms only.
#
# Field list and selector decisions documented in
# docs/data_sources/martindale.md §9.5 with `CONFIRMED 2026-06-01`
# annotations from the recon pass on 5 sample firms.


def _detect_short_description(masthead_items) -> str | None:
    """Item 2 in the masthead is USUALLY the short tagline, but some
    firms (e.g. The McGhee Firm) have no tagline at all and item 2 is
    the Peer-Reviews block. Detect by content, not position.
    """
    for it in masthead_items:
        cls = it.attributes.get("class") or ""
        if "masthead-list__item--bold" in cls:
            continue
        text = it.text(strip=True) or ""
        if not text:
            continue
        if text.startswith("Peer Reviews"):
            continue
        if text.startswith("Profile Visibility"):
            continue
        if any(
            kw in text
            for kw in (
                "Boulevard",
                "Street",
                "Avenue",
                "Suite",
                "Floor",
                "P.O. Box",
                "Drive",
                "Lane",
                "Road",
                "Parkway",
                "Place",
            )
        ):
            continue
        return text
    return None


def _extract_address_line(masthead_items) -> str | None:
    for it in masthead_items:
        cls = it.attributes.get("class") or ""
        if "masthead-list__item--bold" in cls:
            continue
        text = it.text(strip=True) or ""
        if any(
            s in text
            for s in (
                "Boulevard",
                "Street",
                "Avenue",
                "Suite",
                "Floor",
                "P.O. Box",
                "Drive",
                "Lane",
                "Road",
                "Parkway",
                "Place",
            )
        ):
            return text
    return None


def _extract_city_state(masthead_items) -> tuple[str | None, str | None]:
    for it in masthead_items:
        cls = it.attributes.get("class") or ""
        if "masthead-list__item--bold" not in cls:
            continue
        text = it.text(strip=True) or ""
        m = re.match(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s*$", text)
        if m:
            return m.group("city").strip(), m.group("state")
    return None, None


def _extract_zip_from_address(line: str | None) -> str | None:
    """Take the LAST 5-digit ZIP — Martindale sometimes lists two
    (P.O. Box zip + physical zip); we want the physical one."""
    if not line:
        return None
    zips = re.findall(r"\b(\d{5})(?:-\d{4})?\b", line)
    return zips[-1] if zips else None


def _extract_practice_areas(tree: HTMLParser) -> list[str]:
    aop = tree.css_first("ul#aopList")
    if aop is None:
        return []
    return [(li.text(strip=True) or "").strip() for li in aop.css("li") if li.text(strip=True)]


def _extract_toggle_count(tree: HTMLParser, label_prefix: str) -> int | None:
    """toggle-area__header-count h2 text already contains the count
    in parens — e.g. h2 "People(56)" or h2 "Areas of Practice(44)".
    """
    for h2 in tree.css("h2"):
        text = h2.text(strip=True) or ""
        if not text.startswith(label_prefix):
            continue
        m = re.search(r"\((\d+)\)", text)
        if m:
            return int(m.group(1))
    return None


def _walk_dfs(node):
    """Yield `node` and every descendant in depth-first document order.

    selectolax's `Node.iter()` only walks direct children, so we
    recurse manually. Document order is critical here: it's what
    lets us pair each truncate-text div with the preceding h2.
    """
    yield node
    for child in node.iter():
        yield from _walk_dfs(child)


def _extract_descriptions(tree: HTMLParser) -> list[dict[str, Any]]:
    """Pair each `div.truncate-text` with the nearest preceding `h2`
    in document order. Filter the AOP-rendered-as-text noise block.
    """
    out: list[dict[str, Any]] = []
    last_heading: str | None = None
    body = tree.body
    if body is None:
        return out
    for node in _walk_dfs(body):
        if node.tag == "h2":
            last_heading = node.text(strip=True) or None
            continue
        if node.tag == "div":
            cls = node.attributes.get("class") or ""
            if "truncate-text" not in cls:
                continue
            text = node.text(strip=True) or ""
            if not text:
                continue
            # Filter the AOP rendered as text. Two signals together:
            # the parent heading is "Areas of Practice..." OR the text
            # body has fewer than 1 space per 20 chars (concatenated
            # CamelCase areas like "Civil LitigationPersonal Injury...").
            if last_heading and last_heading.startswith("Areas of Practice"):
                continue
            if last_heading and last_heading.startswith("People"):
                continue
            space_density = text.count(" ") / max(len(text), 1)
            if len(text) < 500 and space_density < 0.05:
                continue
            out.append({"heading": last_heading, "text": text})
    return out


def _extract_year_established(tree: HTMLParser) -> int | None:
    for d in tree.css("div"):
        text = d.text(strip=True) or ""
        if text.startswith("Year Established"):
            m = re.search(r"([12][0-9]{3})", text)
            if m:
                return int(m.group(1))
    body_text = tree.body.text(strip=False) if tree.body else ""
    m = re.search(r"Year\s+Established[^A-Za-z0-9]+([12][0-9]{3})", body_text, flags=re.I)
    return int(m.group(1)) if m else None


def _extract_office_size_label(tree: HTMLParser) -> int | None:
    """ "Office Size" on Martindale = firm headcount, NOT number of
    offices. Returned for the people-count cross-check; not used as
    `office_count` directly.
    """
    body_text = tree.body.text(strip=False) if tree.body else ""
    m = re.search(r"Office\s+Size[^A-Za-z0-9]+(\d+)", body_text, flags=re.I)
    return int(m.group(1)) if m else None


def parse_firm_profile_full(payload: bytes) -> dict[str, Any]:
    """Full firm-profile extraction for the enrichment pass.

    `office_count` is returned as None when the only available source
    ("Office Size") is within shouting distance of `people_count` —
    that's the Martindale firm-size synonym, not a true office count.
    """
    html = (
        payload.decode("utf-8", errors="replace")
        if isinstance(payload, (bytes, bytearray))
        else payload
    )
    tree = HTMLParser(html)

    masthead_items = tree.css("ul.masthead-list li.masthead-list__item")
    address_line = _extract_address_line(masthead_items)
    city, state = _extract_city_state(masthead_items)
    zip_code = _extract_zip_from_address(address_line)
    short_desc = _detect_short_description(masthead_items)

    practice_areas = _extract_practice_areas(tree)
    people_count = _extract_toggle_count(tree, "People")
    aop_count_via_toggle = _extract_toggle_count(tree, "Areas of Practice")
    year_established = _extract_year_established(tree)
    office_size_label = _extract_office_size_label(tree)
    descriptions = _extract_descriptions(tree)

    slim = parse_firm_profile(html.encode("utf-8") if isinstance(html, str) else html)

    # Heuristic office_count: only when the "Office Size" value is
    # clearly NOT just a synonym for people-count. Otherwise NULL.
    office_count: int | None = None
    if office_size_label is not None:
        if people_count is None:
            # No people count to compare; trust the label.
            office_count = office_size_label
        else:
            slack = max(2, people_count // 4)
            if abs(office_size_label - people_count) > slack:
                office_count = office_size_label

    return {
        "primary_address_line": address_line,
        "primary_city": city,
        "primary_state": state,
        "primary_postal_code": zip_code,
        "firm_short_description": short_desc,
        "firm_website_url": slim.get("firm_website_url"),
        "firm_website_is_sponsored": slim.get("firm_website_is_sponsored"),
        "firm_phone": slim.get("firm_phone"),
        "practice_areas": practice_areas,
        "practice_area_count_toggle": aop_count_via_toggle,
        "people_count": people_count,
        "year_established": year_established,
        "firm_descriptions": descriptions,
        "office_count": office_count,
        "office_size_label_raw": office_size_label,
    }
