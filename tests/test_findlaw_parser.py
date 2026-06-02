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
