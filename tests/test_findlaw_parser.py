"""FindLaw parser tests — against synthetic HTML and the saved Alabaster
recon fixture extracted JSON (validates against committed data)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from selectolax.parser import HTMLParser

from legal_sourcing.parsers.findlaw import (
    FindLawCityParser,
    _extract_card,
    _parse_location_text,
    extract_page_meta,
)

FIXTURES = Path(__file__).parent / "fixtures" / "findlaw" / "recon"


# ---- _parse_location_text ----------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (
            "31 Inverness Center Pkwy, Suite 350, Birmingham, AL 35242",
            {
                "street_raw": "31 Inverness Center Pkwy, Suite 350",
                "city_raw": "Birmingham",
                "state_raw": "AL",
                "postal_code_raw": "35242",
            },
        ),
        (
            "1 N Central Ave, Phoenix, AZ 85004",
            {
                "street_raw": "1 N Central Ave",
                "city_raw": "Phoenix",
                "state_raw": "AZ",
                "postal_code_raw": "85004",
            },
        ),
        (
            "123 Main St, Mobile, AL 36602-1234",
            {
                "street_raw": "123 Main St",
                "city_raw": "Mobile",
                "state_raw": "AL",
                "postal_code_raw": "36602",  # truncate +4
            },
        ),
        # Partial / unparseable -> street_raw fallback.
        ("just a string", {"street_raw": "just a string"}),
        ("", {}),
        (None, {}),
    ],
)
def test_parse_location_text(raw, expected):
    assert _parse_location_text(raw) == expected


# ---- _extract_card -----------------------------------------------------


def test_extract_card_basic_shape():
    html = """
    <div class="fl-serp-card organic" data-testid="organic-card-3">
      <h2><a class="fl-serp-card-title"
             href="https://lawyers.findlaw.com/alabama/birmingham/mezrano-law-firm-NDkwMzUzOF8x/"
             data-testid="serp-card-title-link">Mezrano Law Firm</a></h2>
      <div class="fl-serp-card-text" data-testid="serp-card-text">
        Car Accidents Lawyers Serving Alabaster, AL
      </div>
      <div class="fl-serp-card-location"><span>31 Inverness Center Pkwy, Suite 350, Birmingham, AL 35242</span></div>
      <div class="fl-serp-card-buttons">
        <a data-testid="website-button-link" href="https://www.mezrano.com/" rel="nofollow">Website</a>
        <a data-testid="phone-button-link" href="tel:+12054627409">Phone</a>
      </div>
    </div>
    """
    tree = HTMLParser(html)
    card = tree.css_first(".fl-serp-card.organic")
    rec = _extract_card(card)
    assert rec is not None
    assert rec["name_raw"] == "Mezrano Law Firm"
    assert rec["website_raw"] == "https://www.mezrano.com/"
    assert rec["phone_raw"] == "+12054627409"
    assert rec["contacts"] == []  # FindLaw doesn't expose attorneys per card
    assert len(rec["offices"]) == 1
    assert rec["offices"][0]["city_raw"] == "Birmingham"
    assert rec["offices"][0]["state_raw"] == "AL"
    assert rec["offices"][0]["postal_code_raw"] == "35242"
    ad = rec["additional_data"]
    assert ad["source_firm_id_findlaw"] == "NDkwMzUzOF8x"
    assert ad["data_testid"] == "organic-card-3"
    assert ad["website_rel"] == "nofollow"


def test_extract_card_without_title_returns_none():
    """Cards missing a title anchor (ad slots / malformed nodes) are
    skipped, not returned with empty fields."""
    html = """
    <div class="fl-serp-card organic">
      <div class="fl-serp-card-location"><span>x, y, AL 12345</span></div>
    </div>
    """
    card = HTMLParser(html).css_first(".fl-serp-card.organic")
    assert _extract_card(card) is None


# ---- FindLawCityParser end-to-end --------------------------------------


def test_city_parser_attaches_practice_area_state_city_slugs():
    html = """<html><body>
      <div class="fl-serp-card organic">
        <a class="fl-serp-card-title"
           href="https://lawyers.findlaw.com/foo/bar-baz-Qm9z/">Bar Firm</a>
        <div class="fl-serp-card-location"><span>1 A St, Phoenix, AZ 85001</span></div>
      </div>
    </body></html>"""
    parser = FindLawCityParser()
    records = parser.parse_bytes(
        html.encode("utf-8"),
        source_url="https://lawyers.findlaw.com/medical-malpractice/arizona/phoenix/",
    )
    assert len(records) == 1
    r = records[0]
    assert r["practice_areas_raw"] == ["medical-malpractice"]
    assert r["additional_data"]["practice_area_slug"] == "medical-malpractice"
    assert r["additional_data"]["state_slug"] == "arizona"
    assert r["additional_data"]["city_slug"] == "phoenix"


# ---- extract_page_meta --------------------------------------------------


def _make_srp(
    *,
    card_count: int = 40,
    last_page: int | None = 2,
    has_next: bool = True,
    results_total: int | None = 76,
) -> str:
    cards = "".join(
        f'<div class="fl-serp-card organic">'
        f'<a class="fl-serp-card-title" href="/a/b-{i}/">F {i}</a>'
        f'<div class="fl-serp-card-location"><span>1 A St, Phoenix, AZ 85001</span></div>'
        f"</div>"
        for i in range(card_count)
    )
    page_links = ""
    if last_page is not None:
        for n in range(1, last_page + 1):
            page_links += f'<li><a href="?page={n}">{n}</a></li>'
    next_html = ""
    if has_next:
        next_html = (
            '<a class="fl-pagination-button" rel="next" '
            'data-testid="fl-pagination-button-next" href="?page=2">Next</a>'
        )
    nav = (
        f'<nav aria-label="Pagination">'
        f'<ol class="fl-pagination-list">{page_links}</ol>'
        f"{next_html}</nav>"
        if last_page is not None
        else ""
    )
    rt = f"<p>Results 1 to {card_count} of {results_total}</p>" if results_total is not None else ""
    return f"<html><body>{rt}{cards}{nav}</body></html>"


def test_extract_page_meta_birmingham_shape():
    meta = extract_page_meta(_make_srp(card_count=40, last_page=2, has_next=True, results_total=36))
    assert meta["card_count"] == 40
    assert meta["last_page"] == 2
    assert meta["has_next"] is True
    assert meta["results_total"] == 36


def test_extract_page_meta_last_page_no_next():
    meta = extract_page_meta(
        _make_srp(card_count=36, last_page=2, has_next=False, results_total=36)
    )
    assert meta["has_next"] is False
    assert meta["card_count"] == 36


def test_extract_page_meta_no_pagination_block():
    """Single-page combos have no nav. The parser must not crash."""
    html = '<html><body><div class="fl-serp-card organic"><a class="fl-serp-card-title" href="/x/">F</a></div></body></html>'
    meta = extract_page_meta(html)
    assert meta == {
        "card_count": 1,
        "has_next": False,
        "last_page": None,
        "results_total": None,
    }


# ---- Against the committed recon fixture (round-trip sanity) -----------


def test_alabaster_recon_fixture_round_trip_card_count():
    """The Phase 5 recon fixture is the extracted JSON, but we can
    double-check the count of records matches what the recon log
    reported (40 cards)."""
    bare = json.loads((FIXTURES / "phase5_alabaster_bare.json").read_text(encoding="utf-8"))
    assert len(bare) == 40
    # First card should be Mezrano Law Firm with website + phone.
    first = bare[0]
    assert first["title_text"] == "Mezrano Law Firm"
    assert first["website_url"] == "https://www.mezrano.com/"
    assert first["phone"] == "+12054627409"


# ---- attorney cards (title = PERSON; firm = parent-link) ----------------


ATTORNEY_CARD_HTML = """
<div class="fl-serp-card attorney organic" aria-label="attorney" data-testid="attorney-card-18">
  <h2><a class="fl-serp-card-title directory_profile"
         href="https://lawyers.findlaw.com/florida/davie/scott-cohen-NTM4NzYyOF8x/"
         data-testid="serp-card-title-link">Scott Cohen</a></h2>
  <a class="fl-list-item-link directory_profile"
     data-testid="fl-serp-card-parent-link"
     href="https://lawyers.findlaw.com/florida/davie/the-schiller-kessler-group-NDE0OTYwOF8x/">The Schiller Kessler Group</a>
  <div class="fl-serp-card-text" data-testid="serp-card-text">
    Workers' Compensation Lawyers Serving Port Saint Lucie, FL (Davie)
  </div>
  <div class="fl-serp-card-location"><span>4640 South University Drive, Davie, FL 33328</span></div>
  <div class="fl-serp-card-buttons">
    <a data-testid="website-button-link" href="https://www.injuredinflorida.com/personal-injury-lawyer/davie-fl/" rel="nofollow">Visit Website</a>
    <a data-testid="phone-button-link" href="tel:+19544882962">954-488-2962</a>
  </div>
</div>
"""


def test_extract_card_attorney_recovers_parent_firm():
    """The Scott Cohen regression: the card title is a PERSON; the firm
    name must come from the parent-link, and the person becomes a
    contact (never the firm name)."""
    card = HTMLParser(ATTORNEY_CARD_HTML).css_first(".fl-serp-card.organic")
    rec = _extract_card(card)
    assert rec is not None
    assert rec["name_raw"] == "The Schiller Kessler Group"
    assert len(rec["contacts"]) == 1
    contact = rec["contacts"][0]
    assert contact["name_raw"] == "Scott Cohen"
    assert contact["source_attorney_id"] == "NTM4NzYyOF8x"
    ad = rec["additional_data"]
    assert ad["card_type"] == "attorney"
    assert ad["source_firm_id_findlaw"] == "NDE0OTYwOF8x"  # the FIRM's id
    assert ad["source_attorney_id_findlaw"] == "NTM4NzYyOF8x"
    assert ad["firm_profile_url"].endswith("the-schiller-kessler-group-NDE0OTYwOF8x/")
    assert ad["attorney_profile_url"].endswith("scott-cohen-NTM4NzYyOF8x/")
    # Website / phone / office still captured from the card.
    assert rec["website_raw"].startswith("https://www.injuredinflorida.com/")
    assert rec["phone_raw"] == "+19544882962"
    assert rec["offices"][0]["city_raw"] == "Davie"
    assert rec["offices"][0]["state_raw"] == "FL"


def test_extract_card_attorney_without_parent_is_unnamed():
    """A parent-less attorney card (solo / unaffiliated) must NOT store
    the person as the firm name — name_raw stays None."""
    html = ATTORNEY_CARD_HTML.replace('data-testid="fl-serp-card-parent-link"', 'data-testid="x"')
    card = HTMLParser(html).css_first(".fl-serp-card.organic")
    rec = _extract_card(card)
    assert rec is not None
    assert rec["name_raw"] is None
    assert rec["contacts"][0]["name_raw"] == "Scott Cohen"
    assert rec["additional_data"]["card_type"] == "attorney"


def test_extract_card_firm_card_type_tagged():
    """Firm cards keep the title as the name and carry card_type='firm'."""
    html = """
    <div class="fl-serp-card organic" aria-label="law firm" data-testid="organic-card-1">
      <h2><a class="fl-serp-card-title"
             href="https://lawyers.findlaw.com/florida/davie/the-schiller-kessler-group-NDE0OTYwOF8x/"
             data-testid="serp-card-title-link">The Schiller Kessler Group</a></h2>
    </div>
    """
    card = HTMLParser(html).css_first(".fl-serp-card.organic")
    rec = _extract_card(card)
    assert rec is not None
    assert rec["name_raw"] == "The Schiller Kessler Group"
    assert rec["contacts"] == []
    assert rec["additional_data"]["card_type"] == "firm"
