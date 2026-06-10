"""FindLaw SRP (search results page) parser.

FindLaw SRPs interleave TWO card types under the same
`.fl-serp-card.organic` class (both firm-shaped for our pipeline):

  * **firm cards** — `class="fl-serp-card organic"`, `aria-label="law
    firm"`, `data-testid="organic-card-N"`. The card title IS the firm
    name.
  * **attorney cards** — `class="fl-serp-card attorney organic"`,
    `aria-label="attorney"`, `data-testid="attorney-card-N"`. The card
    title is a PERSON; the firm they belong to is a separate anchor,
    `a[data-testid="fl-serp-card-parent-link"]`. Historically this
    parser took the title as `name_raw`, which stored ~4.7k attorneys
    AS firms ("Scott Cohen" instead of "The Schiller Kessler Group").
    Now the parent-link supplies `name_raw` and the person becomes a
    `contacts` entry — the same firm-row-per-attorney-card shape the
    Martindale city parser emits. A parent-less attorney card (solo /
    unaffiliated) gets `name_raw=None` so aggregation keys it as its
    own unnamed row rather than fabricating a person-named firm.

The firm typically appears under multiple practice areas — aggregation
by `(name_normalized, primary_office_street_normalized)` collapses
those into one FirmSourceRecord with a union of practice-area slugs.

This parser emits firm-shaped dicts. Each one has:
  * name_raw       — the FIRM name (card title for firm cards,
                     parent-link for attorney cards)
  * website_raw    — from a[data-testid="website-button-link"]
  * phone_raw      — from a[data-testid="phone-button-link"] (or tel:)
  * offices        — single office parsed from .fl-serp-card-location
  * contacts       — the card's attorney (attorney cards only)
  * practice_areas_raw — the FindLaw practice-area slug from the
                     source URL (pipeline supplies it)
  * additional_data — card_type + source_firm_id_findlaw (+ the
                     attorney id/url on attorney cards) + card_text +
                     practice_area / state / city slugs.

Normalization (the `*_normalized` columns and practice-area matching)
happens in the pipeline — same convention as the AZ Bar / Martindale
parsers.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from legal_sourcing.normalize.url import safe_urlparse, strip_self_domain
from legal_sourcing.parsers.base import BaseParser

# Never record FindLaw's own domain as a firm website (would collapse to
# a shared key and cause false website matches in resolution).
_FINDLAW_OWN_DOMAINS = ("findlaw.com",)

# US address regex — capture trailing ZIP + 2-letter state to peel
# city/state/zip off the back of the location string. Cards render
# "<street>, <city>, <ST> <zip>" in the location field.
_LOCATION_TAIL_RE = re.compile(
    r"^(?P<rest>.+?),\s*(?P<city>[^,]+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$"
)
_TITLE_ID_RE = re.compile(r"-([A-Za-z0-9_]+)/?$")  # trailing firm id


def _parse_location_text(text: str | None) -> dict[str, Any]:
    """Split the SRP-card location string into street / city / state /
    zip. Falls back to street-only when the trailing pattern doesn't
    match (some cards may show partial addresses).
    """
    if not text:
        return {}
    s = text.strip()
    m = _LOCATION_TAIL_RE.match(s)
    if m:
        rest = m.group("rest").strip()
        return {
            "street_raw": rest or None,
            "city_raw": m.group("city").strip() or None,
            "state_raw": m.group("state"),
            "postal_code_raw": m.group("zip")[:5],
        }
    return {"street_raw": s}


def _normalize_phone_tel(href: str | None) -> str | None:
    if not href:
        return None
    s = href.replace("tel:", "").strip()
    return s or None


def _profile_id(url: str | None) -> str | None:
    """The trailing slug id of a FindLaw profile URL, e.g.
    "mezrano-law-firm-NDkwMzUzOF8x" -> "NDkwMzUzOF8x"."""
    parsed = safe_urlparse(url) if url else None
    if parsed is None:
        return None
    last_seg = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    m = _TITLE_ID_RE.search(last_seg)
    return m.group(1) if m else None


def _extract_card(card_node) -> dict[str, Any] | None:
    """Convert one `fl-serp-card.organic` node into a firm-shaped dict.

    Returns None if the card has no usable title — those are likely
    advertisement / non-firm slots even though they share the class.
    """
    title_a = card_node.css_first('a.fl-serp-card-title, a[data-testid="serp-card-title-link"]')
    if title_a is None:
        return None
    title_text = title_a.text(strip=True) or None
    if not title_text:
        return None
    title_url = title_a.attributes.get("href")

    # Card type: attorney cards carry the `attorney` class token /
    # aria-label="attorney" / data-testid="attorney-card-N"; firm cards
    # are aria-label="law firm" / data-testid="organic-card-N".
    cls_tokens = (card_node.attributes.get("class") or "").split()
    aria = (card_node.attributes.get("aria-label") or "").strip().lower()
    testid = card_node.attributes.get("data-testid") or ""
    is_attorney = (
        "attorney" in cls_tokens or aria == "attorney" or testid.startswith("attorney-card")
    )

    attorney_name: str | None = None
    attorney_profile_url: str | None = None
    if is_attorney:
        # The title is a PERSON. The firm is the parent-link anchor; a
        # parent-less card is a solo/unaffiliated attorney -> no firm name
        # (never store the person as the firm).
        attorney_name = title_text
        attorney_profile_url = title_url
        parent_a = card_node.css_first('a[data-testid="fl-serp-card-parent-link"]')
        name = (parent_a.text(strip=True) or None) if parent_a is not None else None
        profile_url = (parent_a.attributes.get("href") if parent_a is not None else None) or None
    else:
        name = title_text
        profile_url = title_url

    source_firm_id_findlaw = _profile_id(profile_url)

    card_text_node = card_node.css_first('div.fl-serp-card-text, [data-testid="serp-card-text"]')
    card_text = card_text_node.text(strip=True) if card_text_node is not None else None

    loc_node = card_node.css_first("div.fl-serp-card-location > span, div.fl-serp-card-location")
    location_text = loc_node.text(strip=True) if loc_node is not None else None
    office_fields = _parse_location_text(location_text)

    site_node = card_node.css_first('a[data-testid="website-button-link"]')
    website_url = (
        strip_self_domain(site_node.attributes.get("href"), _FINDLAW_OWN_DOMAINS)
        if site_node is not None
        else None
    )
    website_rel = site_node.attributes.get("rel") if site_node is not None else None

    phone_node = card_node.css_first('a[data-testid="phone-button-link"], a[href^="tel:"]')
    phone = (
        _normalize_phone_tel(phone_node.attributes.get("href")) if phone_node is not None else None
    )

    office: dict[str, Any] | None = None
    if office_fields:
        office = {
            "label": None,
            "is_primary": True,
            "country_raw": "US",
            **office_fields,
        }
        if phone:
            office["phone_raw"] = phone

    additional: dict[str, Any] = {
        "card_type": "attorney" if is_attorney else "firm",
        "card_text": card_text,
        "data_testid": card_node.attributes.get("data-testid"),
    }
    if source_firm_id_findlaw:
        additional["source_firm_id_findlaw"] = source_firm_id_findlaw
    if profile_url:
        additional["firm_profile_url"] = profile_url
    if website_rel:
        additional["website_rel"] = website_rel
    if location_text:
        additional["location_text_raw"] = location_text

    # The card's attorney (attorney cards only) — same contact shape as
    # the Martindale city parser.
    contacts: list[dict[str, Any]] = []
    if is_attorney and attorney_name:
        contact: dict[str, Any] = {
            "name_raw": attorney_name,
            "title": None,
            "phone_raw": phone,
            "source_attorney_id": _profile_id(attorney_profile_url),
            "source_attorney_url": attorney_profile_url,
        }
        contacts.append(contact)
        additional["attorney_profile_url"] = attorney_profile_url
        if contact["source_attorney_id"]:
            additional["source_attorney_id_findlaw"] = contact["source_attorney_id"]

    return {
        "name_raw": name,
        "deactivation_status": None,
        "website_raw": website_url,
        "phone_raw": phone,
        "year_founded": None,
        "attorney_count": None,
        "source_last_updated_at": None,
        "contacts": contacts,
        "offices": [office] if office else [],
        # The practice-area slug is supplied by the pipeline because
        # the parser only sees one page at a time.
        "practice_areas_raw": [],
        "additional_data": additional,
    }


def extract_page_meta(html: str) -> dict[str, Any]:
    """Pagination metadata for a city SRP. Returns:

    * `card_count` — count of `.fl-serp-card.organic` on this page
    * `has_next` — `a.fl-pagination-button[rel="next"]` (and not
      class `disabled`) AND the anchor was rendered
    * `last_page` — largest integer text inside `nav[aria-label=
      "Pagination"]` page-number `<li>` elements, when present
    * `results_total` — UNDER-COUNTS in practice (FindLaw shows
      "Results 1 to 40 of 36" where 36 < pages x pagesize). Captured
      for sanity logging only; not load-bearing.
    """
    tree = HTMLParser(html)
    cards = tree.css(".fl-serp-card.organic")
    card_count = len(cards)

    nav = tree.css_first('nav[aria-label="Pagination"]')
    has_next = False
    last_page: int | None = None
    if nav is not None:
        next_a = nav.css_first(
            'a.fl-pagination-button[rel="next"], a[data-testid="fl-pagination-button-next"]'
        )
        if next_a is not None:
            cls = (next_a.attributes.get("class") or "").lower()
            if "disabled" not in cls:
                has_next = True
        for el in nav.css("a, li"):
            t = el.text(strip=True) or ""
            if t.isdigit():
                n = int(t)
                last_page = n if last_page is None else max(last_page, n)

    # "Results X to Y of N" — scan the body for a forgiving regex.
    body_text = tree.body.text(strip=False) if tree.body else html
    results_total: int | None = None
    m = re.search(r"Results?\s+\d[\d,]*\s+to\s+\d[\d,]*\s+of\s+([\d,]+)", body_text)
    if m:
        try:
            results_total = int(m.group(1).replace(",", ""))
        except ValueError:
            results_total = None

    return {
        "card_count": card_count,
        "has_next": has_next,
        "last_page": last_page,
        "results_total": results_total,
    }


class FindLawCityParser(BaseParser):
    """Parse one FindLaw practice-area-city SRP into firm-shaped dicts."""

    SOURCE_NAME = "findlaw"

    def parse_bytes(self, payload: bytes, *, source_url: str) -> list[dict[str, Any]]:
        html = payload.decode("utf-8", errors="replace")
        tree = HTMLParser(html)

        # Derive the (practice_area, state, city) triple from the URL
        # so each emitted record carries provenance regardless of the
        # page's contents.
        path = urlparse(source_url).path.strip("/").split("/")
        practice_area_slug = path[0] if len(path) >= 1 else ""
        state_slug = path[1] if len(path) >= 2 else ""
        city_slug = path[2] if len(path) >= 3 else ""

        out: list[dict[str, Any]] = []
        for card in tree.css(".fl-serp-card.organic"):
            rec = _extract_card(card)
            if rec is None:
                continue
            rec["practice_areas_raw"] = [practice_area_slug] if practice_area_slug else []
            ad = rec.setdefault("additional_data", {})
            ad["practice_area_slug"] = practice_area_slug
            ad["state_slug"] = state_slug
            ad["city_slug"] = city_slug
            out.append(rec)
        return out
