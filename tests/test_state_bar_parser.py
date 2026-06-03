"""Tests for the generic state-bar parsers (WY reference state).

Real captured fixtures live under tests/fixtures/state_bars/wy/. Edge cases
that are awkward to find a real page for (Physical = "Same as Mailing
Address"; a firm-less attorney) use small synthetic WY-shaped HTML.
"""

from __future__ import annotations

import gzip
from pathlib import Path

from legal_sourcing.parsers.state_bar import parse_detail, parse_list

FIX = Path(__file__).parent / "fixtures" / "state_bars" / "wy"


def _gz(name: str) -> bytes:
    with gzip.open(FIX / name, "rb") as f:
        return f.read()


# ---- list ----------------------------------------------------------------


def test_wy_list_extracts_profile_rows():
    rows = parse_list("wy_list", _gz("list_lastname_a.html.gz"), "https://www.wyomingbar.org")
    assert len(rows) > 20  # the "a" page has ~100 attorneys
    r = rows[0]
    assert set(r) >= {"detail_id", "detail_url", "anchor_text"}
    assert "directory-profile" in r["detail_url"]
    assert r["detail_url"].startswith("https://www.wyomingbar.org")
    # ids look like "6-3436"
    assert "-" in r["detail_id"]
    # deduped
    assert len({x["detail_id"] for x in rows}) == len(rows)


# ---- detail (real fixture) -----------------------------------------------


def test_wy_detail_liberty_law_real_fixture():
    list_row = {
        "detail_id": "6-3436",
        "detail_url": "https://www.wyomingbar.org/special/directory-profile/?id=6-3436",
        "anchor_text": "Mr. J. Craig Abraham Gillette, WY",
    }
    rec = parse_detail(
        "wy_detail",
        _gz("detail_liberty_law.html.gz"),
        base_url="https://www.wyomingbar.org",
        list_row=list_row,
    )
    # firm came from the address block via the firm-marker gate
    assert "Liberty Law" in rec["name_raw"]
    # attorney carried from the list anchor, locality stripped
    assert rec["contacts"][0]["name_raw"] == "Mr. J. Craig Abraham"
    assert rec["contacts"][0]["entity_number"] == "6-3436"
    office = rec["offices"][0]
    assert office["city_raw"] == "Gillette"
    assert office["state_raw"] == "WY"
    assert office["postal_code_raw"] == "82718"
    assert office["street_raw"] and "Lakeway" in office["street_raw"]
    assert rec["phone_raw"] and rec["phone_raw"].startswith("(307)")
    assert rec["source_url"].endswith("id=6-3436")


# ---- detail (synthetic edge cases) ---------------------------------------

_SAME_AS_MAILING = b"""
<table>
 <tr><th class="text-blue">Phone</th><td><a href="tel:(307) 111-2222">(307) 111-2222</a></td></tr>
 <tr><th class="text-blue">Mailing Address</th>
     <td><p>Smith &amp; Jones, LLP<br>123 Main St<br>Casper, WY 82601</p></td></tr>
 <tr><th class="text-blue">Physical Address</th><td><p>Same as Mailing Address</p></td></tr>
 <tr><th class="text-blue">Status</th><td>Active</td></tr>
</table>
"""


def test_wy_detail_same_as_mailing_falls_back_to_mailing():
    rec = parse_detail(
        "wy_detail",
        _SAME_AS_MAILING,
        base_url="https://www.wyomingbar.org",
        list_row={"detail_id": "1-1", "anchor_text": "Ms. Jane Roe Casper, WY"},
    )
    assert rec["name_raw"] == "Smith & Jones, LLP"  # firm via marker
    office = rec["offices"][0]
    assert office["street_raw"] == "123 Main St"  # NOT "Same as Mailing Address"
    assert office["city_raw"] == "Casper"
    assert office["state_raw"] == "WY"
    assert office["postal_code_raw"] == "82601"


_FIRMLESS = b"""
<table>
 <tr><th class="text-blue">Phone</th><td>(307) 333-4444</td></tr>
 <tr><th class="text-blue">Physical Address</th><td><p>456 Elm Ave<br>Laramie, WY 82070</p></td></tr>
</table>
"""


def test_wy_detail_firmless_attorney_has_blank_firm_but_keeps_address():
    rec = parse_detail(
        "wy_detail",
        _FIRMLESS,
        base_url="https://www.wyomingbar.org",
        list_row={"detail_id": "2-2", "anchor_text": "Mr. John Doe Laramie, WY"},
    )
    assert rec["name_raw"] == ""  # no firm marker on the address line
    assert rec["contacts"][0]["name_raw"] == "Mr. John Doe"
    office = rec["offices"][0]
    assert office["street_raw"] == "456 Elm Ave"
    assert office["city_raw"] == "Laramie"
    assert office["postal_code_raw"] == "82070"
