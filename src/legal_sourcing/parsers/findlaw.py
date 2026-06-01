"""FindLaw SRP (search results page) parser.

FindLaw cards are **firm-level** (unlike AZ Bar / Martindale which
emit one card per attorney). Each `div.fl-serp-card.organic` is one
firm listing under a (practice-area, state, city) URL. The firm
typically appears under multiple practice areas — aggregation by
`(name_normalized, primary_office_street_normalized)` collapses
those into one FirmSourceRecord with a union of practice-area slugs.

This parser emits firm-shaped dicts. Each one has:
  * name_raw       — from the card title
  * website_raw    — from a[data-testid="website-button-link"]
  * phone_raw      — from a[data-testid="phone-button-link"] (or tel:)
  * offices        — single office parsed from .fl-serp-card-location
  * contacts       — empty (FindLaw SRPs do not expose individual
                     attorneys)
  * practice_areas_raw — the FindLaw practice-area slug from the
                     source URL (pipeline supplies it)
  * additional_data — source_firm_id_findlaw + card_text +
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

from legal_sourcing.parsers.base import BaseParser

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


def _extract_card(card_node) -> dict[str, Any] | None:
    """Convert one `fl-serp-card.organic` node into a firm-shaped dict.

    Returns None if the card has no usable title — those are likely
    advertisement / non-firm slots even though they share the class.
    """
    title_a = card_node.css_first(
        'a.fl-serp-card-title, a[data-testid="serp-card-title-link"]'
    )
    if title_a is None:
        return None
    name = title_a.text(strip=True) or None
    if not name:
        return None
    profile_url = title_a.attributes.get("href")

    # Firm id is the trailing path segment after the last hyphen, e.g.
    # "mezrano-law-firm-NDkwMzUzOF8x" -> "NDkwMzUzOF8x".
    source_firm_id_findlaw: str | None = None
    if profile_url:
        path = urlparse(profile_url).path.rstrip("/")
        last_seg = path.rsplit("/", 1)[-1]
        m = _TITLE_ID_RE.search(last_seg)
        if m:
            source_firm_id_findlaw = m.group(1)

    card_text_node = card_node.css_first(
        'div.fl-serp-card-text, [data-testid="serp-card-text"]'
    )
    card_text = card_text_node.text(strip=True) if card_text_node is not None else None

    loc_node = card_node.css_first(
        "div.fl-serp-card-location > span, div.fl-serp-card-location"
    )
    location_text = loc_node.text(strip=True) if loc_node is not None else None
    office_fields = _parse_location_text(location_text)

    site_node = card_node.css_first('a[data-testid="website-button-link"]')
    website_url = site_node.attributes.get("href") if site_node is not None else None
    website_rel = (
        site_node.attributes.get("rel") if site_node is not None else None
    )

    phone_node = card_node.css_first(
        'a[data-testid="phone-button-link"], a[href^="tel:"]'
    )
    phone = (
        _normalize_phone_tel(phone_node.attributes.get("href"))
        if phone_node is not None
        else None
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

    return {
        "name_raw": name,
        "deactivation_status": None,
        "website_raw": website_url,
        "phone_raw": phone,
        "year_founded": None,
        "attorney_count": None,
        "source_last_updated_at": None,
        "contacts": [],  # SRP cards don't expose individual attorneys
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
      "Results 1 to 40 of 36" where 36 < pages × pagesize). Captured
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

    def parse_bytes(
        self, payload: bytes, *, source_url: str
    ) -> list[dict[str, Any]]:
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
