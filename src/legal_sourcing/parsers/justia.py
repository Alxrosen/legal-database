"""Justia Lawyer Directory parser.

Justia directory pages (`/lawyers/{state}[/{city}]`) list one
`div.jld-card` per ATTORNEY (like AZ Bar / Martindale SRP cards, not
firm-level like FindLaw). Each card carries the lawyer name, office
address, phone, website, and a human practice-area string — but NO
firm name. The pipeline therefore aggregates lawyers into firm-shaped
records by normalized office street (see docs/assumptions.md).

This parser emits one per-lawyer dict per card:
  * name_raw           — None (Justia listings have no firm name)
  * website_raw        — the lawyer/firm external website, if shown
  * phone_raw          — card phone
  * contacts           — [the single lawyer] (name, phone, profile id/url)
  * offices            — [parsed office address] (street/city/state/zip)
  * practice_areas_raw — split from the card's practice-area text
  * additional_data    — source_profile_id_justia, profile_url, card_kind

Normalization + office aggregation happen in the pipeline, same
convention as the other parsers.
"""

from __future__ import annotations

import re
from typing import Any

from selectolax.parser import HTMLParser

from legal_sourcing.normalize.url import strip_self_domain
from legal_sourcing.parsers.base import BaseParser

# Justia's own domains: justia.com and the justia.lawyer hosted-microsite
# pattern. Both collapse to a shared domain, so they must never be
# recorded as a firm's website (false matches in resolution).
_JUSTIA_OWN_DOMAINS = ("justia.com", "justia.lawyer")

# Last address line: "City, ST 12345" (optionally ZIP+4).
_CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$"
)
# Trailing numeric id on a /lawyer/{slug}-{id} profile URL.
_PROFILE_ID_RE = re.compile(r"-(\d+)/?$")
# Non-practice-area noise that shows up in `.outline` on premium cards.
_OUTLINE_NOISE = ("serving", "years of experience", "attorney with", "free consultation")


def _clean_lines_from_br(node) -> list[str]:
    """Split a node's inner HTML on <br> into clean, non-empty lines."""
    if node is None:
        return []
    raw = re.sub(r"(?i)<br\s*/?>", "\n", node.html or "")
    text = HTMLParser(raw).text()
    out = []
    for ln in text.split("\n"):
        ln = re.sub(r"\s+", " ", ln).strip(" \t,")
        if ln:
            out.append(ln)
    return out


def _parse_address(node) -> dict[str, Any]:
    """Parse the `.address` block (br-separated) into raw address fields."""
    lines = _clean_lines_from_br(node)
    if not lines:
        return {}
    # Find the City, ST ZIP line (usually last); street = lines before it.
    csz_idx = None
    for i, ln in enumerate(lines):
        if _CITY_STATE_ZIP_RE.match(ln):
            csz_idx = i
    if csz_idx is None:
        return {"street_raw": " ".join(lines) or None}
    m = _CITY_STATE_ZIP_RE.match(lines[csz_idx])
    street = " ".join(lines[:csz_idx]).strip()
    return {
        "street_raw": street or None,
        "city_raw": m.group("city").strip() or None,
        "state_raw": m.group("state"),
        "postal_code_raw": m.group("zip")[:5],
    }


def _practice_areas(card) -> list[str]:
    """Pull practice areas from the card's `.outline` text(s)."""
    out: list[str] = []
    for node in card.css(".outline"):
        txt = node.text(strip=True)
        if not txt:
            continue
        low = txt.lower()
        if any(n in low for n in _OUTLINE_NOISE):
            continue
        # "A, B and C" / "A and B" -> [A, B, C]
        parts = re.split(r",|\band\b", txt)
        for p in parts:
            p = p.strip()
            if p and p.lower() not in ("", "more"):
                out.append(p)
    # de-dupe, preserve order
    seen: set[str] = set()
    uniq = []
    for p in out:
        if p.lower() not in seen:
            seen.add(p.lower())
            uniq.append(p)
    return uniq


def _website(card) -> str | None:
    # Only emit a firm's own external site — never a Justia domain (those
    # collapse to a shared key and cause false website matches). Keep
    # scanning past a Justia-domain link in case a real one follows.
    for a in card.css("a"):
        href = a.attributes.get("href") or ""
        if not href.startswith("http"):
            continue
        blob = ((a.attributes.get("class") or "") + " " + a.text()).lower()
        if "website" in blob and strip_self_domain(href, _JUSTIA_OWN_DOMAINS):
            return href
    return None


def _extract_card(card) -> dict[str, Any] | None:
    cls = card.attributes.get("class") or ""
    card_kind = "premium" if "-premium" in cls else "organic"

    # Profile URL + stable id.
    profile_url = None
    for a in card.css('a[href*="/lawyer/"]'):
        href = a.attributes.get("href") or ""
        if href and not href.rstrip("/").endswith("/contact"):
            profile_url = href
            break
    profile_id = card.attributes.get("data-vars-profile")
    if not profile_id and profile_url:
        m = _PROFILE_ID_RE.search(profile_url)
        profile_id = m.group(1) if m else None

    name_node = card.css_first("strong.name a, .name a, .name")
    lawyer_name = name_node.text(strip=True) if name_node is not None else None
    if not lawyer_name and not profile_id:
        return None  # not a real lawyer card

    phone_node = card.css_first('a[href^="tel:"], .phone')
    phone = phone_node.text(strip=True) if phone_node is not None else None
    if phone:
        phone = phone.strip() or None

    office_fields = _parse_address(card.css_first(".address"))
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

    contact = {
        "name_raw": lawyer_name,
        "phone_raw": phone,
        "profile_url": profile_url,
    }
    if profile_id:
        contact["source_attorney_id"] = profile_id

    additional: dict[str, Any] = {"card_kind": card_kind}
    if profile_id:
        additional["source_profile_id_justia"] = profile_id
    if profile_url:
        additional["profile_url"] = profile_url

    return {
        "name_raw": None,  # Justia listings carry no firm name
        "deactivation_status": None,
        "website_raw": _website(card),
        "phone_raw": phone,
        "year_founded": None,
        "attorney_count": None,
        "source_last_updated_at": None,
        "contacts": [contact],
        "offices": [office] if office else [],
        "practice_areas_raw": _practice_areas(card),
        "additional_data": additional,
    }


def extract_page_meta(html: str) -> dict[str, Any]:
    """Pagination metadata for a directory page.

    * `card_count` — number of `div.jld-card` on the page.
    * `has_next` — whether a pagination "Next" link is present. Justia's
      next link has NO `rel` attribute (it's an `<a>` with text "Next"
      and a `?page=N` href inside `.pagination`), so we detect it by
      text. Justia caps state pagination (high pages redirect to www
      with no Next link), so this is the stop signal — plus the pipeline
      also stops on an off-subdomain redirect.
    """
    tree = HTMLParser(html)
    cards = tree.css("div.jld-card")
    has_next = False
    for a in tree.css("a"):
        href = a.attributes.get("href") or ""
        if a.text(strip=True).lower() == "next" and "page=" in href:
            has_next = True
            break
    return {"card_count": len(cards), "has_next": has_next}


class JustiaDirectoryParser(BaseParser):
    """Parse one Justia directory page into per-lawyer dicts."""

    SOURCE_NAME = "justia"

    def parse_bytes(self, payload: bytes, *, source_url: str) -> list[dict[str, Any]]:
        html = payload.decode("utf-8", errors="replace")
        tree = HTMLParser(html)
        out: list[dict[str, Any]] = []
        for card in tree.css("div.jld-card"):
            rec = _extract_card(card)
            if rec is not None:
                out.append(rec)
        return out
