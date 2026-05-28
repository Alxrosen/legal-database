"""AZ Bar parser tests — run against the recon fixtures committed
under tests/fixtures/az_bar/recon/.

Covers:
  * List parser: 25 attorneys, shape correct, contacts populated.
  * Detail parser: rich shape including the nested
    AreasOfLawAndPractice flattening.
  * The pipeline's normalize + aggregate helpers exercised on
    synthetic input so the firm-grain aggregation is locked in
    against a focused fixture (independent of network state).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legal_sourcing.parsers.az_bar import (
    AZBarDetailParser,
    AZBarListParser,
    _flatten_areas_of_law,
)
from legal_sourcing.pipelines.scrape_az_bar import (
    aggregate_by_firm,
    normalize_record,
)

FIXTURES = Path(__file__).parent / "fixtures" / "az_bar" / "recon"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---- List parser -----------------------------------------------------


def test_list_parser_returns_one_dict_per_attorney():
    parser = AZBarListParser()
    records = parser.parse_bytes(
        _load("list_page_0001.json"),
        source_url="https://api-proxy.azbar.org/MemberSearch/Search/?Page=1",
    )
    assert len(records) == 25


def test_list_parser_record_shape():
    parser = AZBarListParser()
    records = parser.parse_bytes(
        _load("list_page_0001.json"),
        source_url="https://api-proxy.azbar.org/MemberSearch/Search/?Page=1",
    )
    first = records[0]
    # Firm-shaped output (single-attorney firm; pipeline aggregates).
    assert "name_raw" in first
    assert "contacts" in first and len(first["contacts"]) == 1
    assert "offices" in first
    assert "practice_areas_raw" in first
    # The list payload has no AreasOfLawAndPractice — should be empty.
    assert first["practice_areas_raw"] == []

    contact = first["contacts"][0]
    assert contact["bar_state"] == "AZ"
    # Per-attorney identity carried into the contact extras.
    assert "entity_number" in contact
    assert contact["title"] is None  # AZ Bar doesn't expose titles


def test_list_parser_skips_unsuccessful_envelopes():
    bad = json.dumps(
        {"IsSuccess": False, "Error": "boom", "Result": {"Results": []}}
    ).encode("utf-8")
    parser = AZBarListParser()
    assert parser.parse_bytes(bad, source_url="x") == []


# ---- Detail parser ---------------------------------------------------


def test_detail_parser_returns_one_record():
    parser = AZBarDetailParser()
    records = parser.parse_bytes(
        _load("detail_48199.json"),
        source_url="https://api-proxy.azbar.org/MemberSearch/Search?EntityNumber=48199",
    )
    assert len(records) == 1


def test_detail_parser_extracts_firm_identity():
    parser = AZBarDetailParser()
    [rec] = parser.parse_bytes(
        _load("detail_48199.json"),
        source_url="https://api-proxy.azbar.org/MemberSearch/Search?EntityNumber=48199",
    )
    # Fixture is Anders Aannestad at Aannestad Andelin & Corn LLP.
    assert "Aannestad Andelin" in (rec["name_raw"] or "")
    assert rec["website_raw"] == "www.aac.law"
    contact = rec["contacts"][0]
    assert contact["first_name"] == "Anders"
    assert contact["last_name"] == "Aannestad"
    assert contact["bar_number"] == "018721"
    assert contact["bar_state"] == "AZ"
    # Detail-only enrichment fields.
    assert contact.get("admitted_year") == "1997"
    assert contact.get("law_school") == "U of Arizona"
    assert contact.get("languages") == ["Norwegian"]


def test_detail_parser_flattens_nested_areas_of_law():
    parser = AZBarDetailParser()
    [rec] = parser.parse_bytes(
        _load("detail_48199.json"),
        source_url="x",
    )
    raw = rec["practice_areas_raw"]
    # Top-level category from the fixture.
    assert "Intellectual Property" in raw
    # Sub-areas appear too.
    assert "Copyright Litigation" in raw
    assert "Trademark Registration" in raw or any("Trademark" in s for s in raw)


def test_flatten_handles_empty_and_list_inputs():
    assert _flatten_areas_of_law(None) == []
    assert _flatten_areas_of_law({}) == []
    assert _flatten_areas_of_law(["IP", "Family Law"]) == ["IP", "Family Law"]


# ---- Normalize + aggregate -------------------------------------------


def test_normalize_record_populates_normalized_fields():
    parser = AZBarDetailParser()
    [rec] = parser.parse_bytes(_load("detail_48199.json"), source_url="x")
    normalize_record(rec)
    # Firm-level normalizations.
    assert rec["name_normalized"] is not None
    assert "aannestad" in rec["name_normalized"]
    assert rec["website_normalized"] == "aac.law"
    # Practice areas: at least "Intellectual Property" maps to the
    # canonical "intellectual-property" slug.
    assert "intellectual-property" in rec["practice_areas_matched"]


def test_normalize_record_handles_missing_address():
    """Some attorneys in the directory have empty Address fields. The
    normalize step must not raise on missing offices.
    """
    parser = AZBarListParser()
    records = parser.parse_bytes(_load("list_page_0001.json"), source_url="x")
    # Find one without an office (or use the first; either way it must not raise).
    for r in records:
        normalize_record(r)


def test_aggregate_groups_same_firm_same_street():
    """Two attorneys at the same firm + same street collapse to one
    record with two contacts."""
    parser = AZBarDetailParser()
    [base_record] = parser.parse_bytes(_load("detail_48199.json"), source_url="x")
    normalize_record(base_record)
    # Synthesize a second attorney at the same firm/office.
    twin = json.loads(json.dumps(base_record))  # deep copy via JSON
    twin["contacts"][0]["entity_number"] = 99999
    twin["contacts"][0]["bar_number"] = "099999"
    twin["contacts"][0]["first_name"] = "Imaginary"
    twin["contacts"][0]["last_name"] = "Partner"
    twin["contacts"][0]["name_raw"] = "Imaginary Partner"

    agg = aggregate_by_firm([base_record, twin])
    assert len(agg) == 1
    assert agg[0]["attorney_count"] == 2
    last_names = {c.get("last_name") for c in agg[0]["contacts"]}
    assert last_names == {"Aannestad", "Partner"}
    # source_firm_id is stable for re-runs.
    assert agg[0]["source_firm_id"]
    assert len(agg[0]["source_firm_id"]) == 16


def test_aggregate_splits_different_firms():
    """Different name OR different normalized street -> separate rows."""
    parser = AZBarDetailParser()
    [r1] = parser.parse_bytes(_load("detail_48199.json"), source_url="x")
    normalize_record(r1)
    r2 = json.loads(json.dumps(r1))
    # Change the firm name -> different group.
    r2["name_raw"] = "Different Firm LLP"
    normalize_record(r2)

    agg = aggregate_by_firm([r1, r2])
    assert len(agg) == 2


def test_aggregate_does_not_collapse_unaffiliated_attorneys():
    """Regression: pilot run #1 collapsed 8 attorneys with empty
    Company into a single row because their aggregation key was
    (None, None). Each unaffiliated attorney must now be its own row,
    keyed on EntityNumber.
    """
    # Build two distinct attorneys with NO firm name and NO address.
    def _solo(entity_number: int, last_name: str) -> dict:
        return {
            "name_raw": None,
            "name_normalized": None,
            "website_raw": None,
            "website_normalized": None,
            "phone_raw": None,
            "phone_normalized": None,
            "contacts": [
                {
                    "name_raw": f"Test {last_name}",
                    "first_name": "Test",
                    "last_name": last_name,
                    "entity_number": entity_number,
                    "bar_number": f"{entity_number:06d}",
                    "bar_state": "AZ",
                }
            ],
            "offices": [],
            "practice_areas_raw": [],
            "practice_areas_matched": [],
            "practice_areas_unmatched": [],
            "additional_data": {},
        }

    a = _solo(1001, "Aaronson")
    b = _solo(1002, "Brown")
    agg = aggregate_by_firm([a, b])
    assert len(agg) == 2, "two unaffiliated attorneys should NOT merge"
    assert agg[0]["source_firm_id"] != agg[1]["source_firm_id"]
    assert agg[0]["attorney_count"] == 1
    assert agg[1]["attorney_count"] == 1
