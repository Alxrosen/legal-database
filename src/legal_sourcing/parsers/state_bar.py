"""Generic state-bar parsers.

State-bar directories vary too much for one set of CSS selectors, but they
do NOT each deserve a bespoke scraper. So the request/sweep/pagination is
declared in ``state_bars.StateBarConfig`` and the two genuinely
source-specific steps — turning a results page into rows, and a detail page
into a FirmSourceRecord-shaped dict — are small named functions registered
here. Adding a state is: one config entry + (usually) two short functions
that reuse the shared helpers below.

These are PURE functions of their HTML input (+ the list row they came from):
no network, no DB, no clock. Normalization happens later in the pipeline.

Emitted record shape matches what ``pipelines.scrape_az_bar.normalize_record``
and ``aggregate_by_firm`` expect: ``name_raw`` carries the FIRM name (blank
when the source has no firm), the attorney goes in ``contacts``, and the
office address goes in ``offices``. Aggregation then groups attorneys into
firm rows (and keeps firm-less attorneys as stable solo rows).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from legal_sourcing.normalize.name import looks_like_firm

# "City, ST 12345" or "City, ST 12345-6789" — the canonical last address line.
_CITY_STATE_ZIP = re.compile(r"^(.*),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$")
_PO_BOX = re.compile(r"^\s*(p\.?\s*o\.?\s*box|post office box)\b", re.I)


# ---------------------------------------------------------------------------
# Shared helpers


def _td_lines(td: Any) -> list[str]:
    """Split a <td> whose value uses <br>/<p> into clean text lines."""
    if td is None:
        return []
    lines: list[str] = []
    blocks = td.css("p") or [td]
    for b in blocks:
        for part in re.split(r"<br\s*/?>", b.html or ""):
            txt = " ".join(HTMLParser(part).text().split())
            if txt:
                lines.append(txt)
    return lines


def _label_value_table(tree: HTMLParser, label_selector: str) -> dict[str, Any]:
    """Map ``label -> <td> node`` for a profile table whose rows are
    ``<th>Label</th><td>value</td>``. ``label_selector`` selects the <th>.
    """
    out: dict[str, Any] = {}
    for th in tree.css(label_selector):
        label = " ".join((th.text() or "").split())
        if not label:
            continue
        td = th.next
        while td is not None and getattr(td, "tag", "") != "td":
            td = td.next
        if td is not None:
            out[label] = td
    return out


def _split_address(lines: list[str]) -> dict[str, str | None]:
    """From free address lines, pull (firm-if-any, street, city, state, zip).

    The last line matching "City, ST ZIP" is the locality; lines before it
    are street (a leading firm-looking line is reported separately, not as
    street). PO boxes are kept as street when no physical street exists.
    """
    firm: str | None = None
    city = state = postal = None
    street_parts: list[str] = []
    body = list(lines)

    # Locate + consume the City, ST ZIP line (scan from the bottom).
    for i in range(len(body) - 1, -1, -1):
        m = _CITY_STATE_ZIP.match(body[i])
        if m:
            city, state, postal = m.group(1).strip(), m.group(2), m.group(3)
            body = body[:i] + body[i + 1 :]
            break

    for ln in body:
        if firm is None and looks_like_firm(ln) and not _PO_BOX.match(ln):
            firm = ln
            continue
        street_parts.append(ln)

    street = ", ".join(street_parts) or None
    return {"firm": firm, "street": street, "city": city, "state": state, "postal": postal}


def _clean_phone(td: Any) -> str | None:
    if td is None:
        return None
    txt = " ".join((td.text() or "").split())
    return txt or None


# ---------------------------------------------------------------------------
# Wyoming


def wy_list(html: bytes, base_url: str) -> list[dict[str, Any]]:
    """WY results: anchors to ``/special/directory-profile/?id=X-XXXX`` whose
    text is "Name City, ST". One per attorney; dedup by id.
    """
    tree = HTMLParser(html)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in tree.css('a[href*="directory-profile"]'):
        href = a.attributes.get("href") or ""
        m = re.search(r"[?&]id=([\w-]+)", href)
        if not m:
            continue
        wy_id = m.group(1)
        if wy_id in seen:
            continue
        seen.add(wy_id)
        rows.append(
            {
                "detail_id": wy_id,
                "detail_url": urljoin(base_url, href),
                "anchor_text": " ".join((a.text() or "").split()),
            }
        )
    return rows


def _name_from_anchor(anchor: str, city: str | None, state: str | None) -> str:
    """The WY list anchor is "Name City, ST"; strip the trailing locality."""
    name = anchor
    if state:
        name = re.sub(rf",\s*{re.escape(state)}\s*$", "", name)
    if city and name.endswith(city):
        name = name[: -len(city)]
    return name.strip().rstrip(",").strip()


def wy_detail(html: bytes, *, base_url: str, list_row: dict[str, Any]) -> dict[str, Any]:
    """Parse a WY directory profile into a firm-shaped record.

    WY has no firm field; firm is taken from an address line ONLY when it
    looks like a firm. Otherwise the record is a firm-less attorney row that
    still resolves via its street address + phone.
    """
    tree = HTMLParser(html)
    fields = _label_value_table(tree, "th.text-blue")

    phone = _clean_phone(fields.get("Phone"))
    physical = _td_lines(fields.get("Physical Address"))
    mailing = _td_lines(fields.get("Mailing Address"))
    status_txt = (
        " ".join((fields.get("Status").text() or "").split()) if fields.get("Status") else ""
    )

    # Choose the office block: prefer Physical, but it is frequently just a
    # pointer ("Same as Mailing Address") rather than a real address — in that
    # case (or when empty) fall back to Mailing.
    phys_is_pointer = bool(physical) and "same as" in " ".join(physical).lower()
    addr_lines = mailing if (not physical or phys_is_pointer) else physical
    addr = _split_address(addr_lines)

    # Firm name (WY has no firm field): the first firm-looking line of EITHER
    # block. Gated by looks_like_firm so a person's name never becomes a firm.
    firm = addr["firm"]
    if firm is None:
        for blk in (mailing, physical):
            if blk and looks_like_firm(blk[0]) and not _PO_BOX.match(blk[0]):
                firm = blk[0]
                break

    attorney = _name_from_anchor(list_row.get("anchor_text", ""), addr["city"], addr["state"])

    deactivation = None
    low_status = status_txt.lower()
    if any(w in low_status for w in ("inactive", "retired", "deceased", "suspended", "disbarred")):
        deactivation = next(
            w
            for w in ("inactive", "retired", "deceased", "suspended", "disbarred")
            if w in low_status
        )

    office = {
        "is_primary": True,
        "street_raw": addr["street"],
        "city_raw": addr["city"],
        "state_raw": addr["state"] or "WY",
        "postal_code_raw": addr["postal"],
        "phone_raw": phone,
    }
    contact = {
        "name_raw": attorney or None,
        "title": None,
        "phone_raw": phone,
        "entity_number": list_row.get("detail_id"),
        "source_attorney_id": list_row.get("detail_id"),
    }
    return {
        "source_url": list_row.get("detail_url") or base_url,
        "name_raw": firm or "",
        "website_raw": None,
        "phone_raw": phone,
        "deactivation_status": deactivation,
        "contacts": [contact],
        "offices": [office],
        "practice_areas_raw": [],
        "additional_data": {
            "state_bar_id": list_row.get("detail_id"),
            "status": status_txt or None,
            "mailing_address_lines": mailing,
        },
    }


# ---------------------------------------------------------------------------
# Registry + dispatch

_LIST_EXTRACTORS: dict[str, Callable[[bytes, str], list[dict[str, Any]]]] = {
    "wy_list": wy_list,
}
_DETAIL_EXTRACTORS: dict[str, Callable[..., dict[str, Any]]] = {
    "wy_detail": wy_detail,
}


def parse_list(extractor: str, html: bytes, base_url: str) -> list[dict[str, Any]]:
    return _LIST_EXTRACTORS[extractor](html, base_url)


def parse_detail(
    extractor: str, html: bytes, *, base_url: str, list_row: dict[str, Any]
) -> dict[str, Any]:
    return _DETAIL_EXTRACTORS[extractor](html, base_url=base_url, list_row=list_row)


__all__ = ["parse_detail", "parse_list", "wy_detail", "wy_list"]
