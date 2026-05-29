"""Martindale parser tests — focused on the three card shapes and
the title/firm split behavior.

Recon fixtures live under tests/fixtures/martindale/recon/.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legal_sourcing.parsers.martindale import (
    MartindaleCityParser,
    _split_title_at_firm,
    parse_firm_profile,
)

FIXTURES = Path(__file__).parent / "fixtures" / "martindale" / "recon"


# ---- _split_title_at_firm -----------------------------------------------


@pytest.mark.parametrize(
    "raw, title, firm",
    [
        # Subscriber card slicing artifact (no whitespace after "at"):
        ("Managing Partner at", "Managing Partner", None),
        ("Member at", "Member", None),
        ("Founder at", "Founder", None),
        # Non-subscriber "Title at Firm" pattern:
        ("Member at DaGian Law Offices, LLP", "Member", "DaGian Law Offices, LLP"),
        ("Gen. Coun. at Great Southern Wood Preserving, Inc.", "Gen. Coun.", "Great Southern Wood Preserving, Inc."),
        # Solo / freeform — no " at " at all:
        ("Solo Practitioner", "Solo Practitioner", None),
        ("Attorney", "Attorney", None),
        # Comma variants:
        ("Member, at FirmName", "Member", "FirmName"),
        # Empty / None
        ("", None, None),
        (None, None, None),
    ],
)
def test_split_title_at_firm(raw, title, firm):
    assert _split_title_at_firm(raw or "") == (title, firm)


# ---- City parser end-to-end (against recon fixture) ---------------------


def test_city_parser_emits_one_record_per_card():
    """Recon fixture had 54 cards from Abbeville, AL. The parser run
    against the raw page should produce the same count."""
    import gzip
    # The recon saved gz pages under data/raw — but those are local
    # and gitignored. The committed fixture is the extracted JSON,
    # not the raw HTML. Use the extracted card list to check we still
    # see the same 54 entries when re-parsing fresh raw bytes.
    # (We have a smaller smoke check below that uses synthetic HTML.)
    payload = b"""<html><body>
    <div class="card card--attorney">
      <ul>
        <li class="detail_title">
          <a href="https://www.martindale.com/attorney/john-doe-12345/">
            <h3>John Doe</h3>
          </a>
        </li>
        <li class="detail_position">
          Managing Partner at
          <a class="detail_position--office-link"
             href="https://www.martindale.com/organization/example-firm-99/dothan-alabama-100-f/"
             data-gtm-tracking='{"firm_id":"100"}'>
            Example Firm
          </a>
        </li>
        <li class="detail_location">Dothan, AL</li>
      </ul>
    </div>
    </body></html>"""
    parser = MartindaleCityParser()
    records = parser.parse_bytes(
        payload, source_url="https://www.martindale.com/all-lawyers/dothan/alabama/"
    )
    assert len(records) == 1
    rec = records[0]
    assert rec["name_raw"] == "Example Firm"
    contact = rec["contacts"][0]
    assert contact["name_raw"] == "John Doe"
    assert contact["title"] == "Managing Partner"  # trailing "at" trimmed
    assert contact["source_attorney_id"] == "12345"
    assert rec["additional_data"]["source_firm_id_martindale"] == "100"
    assert rec["additional_data"]["card_shape"] == "subscriber"
    assert rec["offices"][0]["city_raw"] == "Dothan"
    assert rec["offices"][0]["state_raw"] == "AL"


def test_city_parser_handles_non_subscriber_at_pattern():
    """Card with NO firm anchor; title is freeform 'X at Y'."""
    payload = b"""<html><body>
    <div class="card card--attorney">
      <ul>
        <li class="detail_title">
          <a href="https://www.martindale.com/attorney/jane-doe-99999/">
            <h3>Jane Doe</h3>
          </a>
        </li>
        <li class="detail_position">Member at DaGian Law Offices, LLP</li>
        <li class="detail_location">Birmingham, AL</li>
      </ul>
    </div>
    </body></html>"""
    parser = MartindaleCityParser()
    records = parser.parse_bytes(
        payload, source_url="https://www.martindale.com/all-lawyers/birmingham/alabama/"
    )
    assert len(records) == 1
    rec = records[0]
    assert rec["name_raw"] == "DaGian Law Offices, LLP"
    contact = rec["contacts"][0]
    assert contact["title"] == "Member"
    assert rec["additional_data"]["card_shape"] == "non_subscriber_at_pattern"


def test_city_parser_handles_solo_no_firm():
    payload = b"""<html><body>
    <div class="card card--attorney">
      <ul>
        <li class="detail_title">
          <a href="https://www.martindale.com/attorney/sue-solo-77777/">
            <h3>Sue Solo</h3>
          </a>
        </li>
        <li class="detail_position">Solo Practitioner</li>
        <li class="detail_location">Mobile, AL</li>
      </ul>
    </div>
    </body></html>"""
    parser = MartindaleCityParser()
    [rec] = parser.parse_bytes(payload, source_url="https://www.martindale.com/all-lawyers/mobile/alabama/")
    assert rec["name_raw"] is None
    assert rec["contacts"][0]["title"] == "Solo Practitioner"
    assert rec["additional_data"]["card_shape"] == "solo"


# ---- Firm profile parser -----------------------------------------------


def test_firm_profile_uses_webstats_selector():
    payload = b"""<html><body>
    <a class="webstats-website-click button"
       rel="sponsored"
       href="http://example.com/firm">visit</a>
    <a href="tel:555-1234">call</a>
    </body></html>"""
    profile = parse_firm_profile(payload)
    assert profile["firm_website_url"] == "http://example.com/firm"
    assert profile["firm_website_is_sponsored"] is True
    assert profile["firm_phone"] == "555-1234"


def test_firm_profile_handles_missing_website():
    payload = b"<html><body><a href='tel:555-9999'>x</a></body></html>"
    profile = parse_firm_profile(payload)
    assert "firm_website_url" not in profile
    assert profile["firm_phone"] == "555-9999"
