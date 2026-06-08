"""Tests for the website-as-a-source FSR record shaping (enrich_websites.load-fsr).

`fsr_record_from_site` maps a SiteExtraction -> a FirmSourceRecord upsert dict
(source="website"). These are pure-function tests (no DB / no network): they pin
the emit filter, the keys/provenance, the offices shape, the practice-area
matched/unmatched split, the assembled additional_data, and the normalized merge
keys (so a website row keys identically to a firm's other source rows).
"""

from __future__ import annotations

from datetime import UTC, datetime

from legal_sourcing.enrichment.website_extract import SiteExtraction
from legal_sourcing.pipelines.enrich_websites import fsr_record_from_site

_TS = datetime(2026, 6, 8, tzinfo=UTC)


def _shape(site: SiteExtraction, website: str = "smithlaw.com") -> dict | None:
    return fsr_record_from_site(
        website,
        site,
        source_url=f"https://{website}",
        http_status=200,
        raw_payload_path=f"/raw/{website}/home.html.gz",
        scraped_at=_TS,
    )


def test_fsr_record_emits_only_real_firm_sites():
    # Non-firm verdicts never become firm rows (would fabricate junk).
    for status in ("not_a_law_firm", "unreachable", "government_or_edu", "unverified"):
        assert _shape(SiteExtraction(url_verification_status=status, name_raw="X Law")) is None
    # Real firm sites are emitted.
    for status in ("verified", "legal_but_mismatched"):
        assert _shape(SiteExtraction(url_verification_status=status, name_raw="X Law LLP")) is not None


def test_fsr_record_keys_and_provenance():
    site = SiteExtraction(url_verification_status="verified", name_raw="Smith & Jones, LLP")
    rec = _shape(site, website="smithjones.com")
    assert rec is not None
    assert rec["source"] == "website"
    assert rec["source_firm_id"] == "smithjones.com"  # bare domain, the dedupe key
    assert rec["source_url"] == "https://smithjones.com"
    assert rec["raw_payload_path"].endswith("home.html.gz")
    assert rec["http_status"] == 200
    assert rec["scraped_at"] == _TS
    assert rec["name_raw"] == "Smith & Jones, LLP"


def test_fsr_record_offices_shape_and_primary():
    site = SiteExtraction(
        url_verification_status="verified",
        name_raw="Multi Office Law LLP",
        office_addresses=[
            {"city": "Phoenix", "state": "AZ", "postal_code": "85004"},
            {"city": "Tucson", "state": "AZ", "postal_code": "85701"},
        ],
        office_count=2,
        primary_city="Phoenix",
        primary_state="AZ",
        primary_postal_code="85004",
    )
    rec = _shape(site)
    assert rec is not None
    assert len(rec["offices"]) == 2
    o0 = rec["offices"][0]
    assert o0["city_raw"] == "Phoenix"
    assert o0["state_raw"] == "AZ"
    assert o0["postal_code_raw"] == "85004"
    assert o0["is_primary"] is True
    assert rec["offices"][1]["is_primary"] is False
    # normalize_record fills the normalized sub-dict (matches other sources).
    assert o0["normalized"] == {
        "city": "Phoenix",
        "state": "AZ",
        "postal_code": "85004",
        "country": "US",
    }
    assert rec["primary_city"] == "Phoenix"
    assert rec["primary_state"] == "AZ"
    assert rec["office_count"] == 2


def test_fsr_record_practice_areas_combined_and_split():
    site = SiteExtraction(
        url_verification_status="verified",
        name_raw="PI Law LLP",
        practice_areas=["personal-injury"],
        practice_areas_raw=["Personal Injury"],
        practice_areas_unmatched=["equine law"],
    )
    rec = _shape(site)
    assert rec is not None
    # raw holds ALL advertised phrases (matched + URL-declared), verbatim
    assert "Personal Injury" in rec["practice_areas_raw"]
    assert "equine law" in rec["practice_areas_raw"]
    # normalize_record re-derives canonical matched slugs + normalized unmatched
    assert "personal-injury" in rec["practice_areas_matched"]
    assert rec["practice_areas_unmatched"]  # the URL-declared unknown survived
    assert any("equine" in u for u in rec["practice_areas_unmatched"])


def test_fsr_record_additional_data_carries_site_tech():
    site = SiteExtraction(
        url_verification_status="legal_but_mismatched",
        name_raw="Mismatch Law LLP",
        platform="wordpress",
        scope="national",
        notable_signals=["super_lawyers"],
        needs_render=False,
        attorney_count=50,
        attorney_count_method="stated",
        attorney_count_confidence="high",
    )
    rec = _shape(site)
    assert rec is not None
    ad = rec["additional_data"]
    assert ad["enrichment_source"] == "website"
    assert ad["url_verification_status"] == "legal_but_mismatched"
    assert ad["platform"] == "wordpress"
    assert ad["scope"] == "national"
    assert ad["notable_signals"] == ["super_lawyers"]
    assert ad["attorney_count_method"] == "stated"


def test_fsr_record_normalized_merge_keys():
    # The merge keys must be derived the SAME way as martindale rows (shared
    # normalize_record) so the website row clusters with the firm's other sources.
    site = SiteExtraction(
        url_verification_status="verified",
        name_raw="Smith & Jones, LLP",
        phones=["+16025551234"],
    )
    rec = _shape(site, website="smithjones.com")
    assert rec is not None
    assert rec["website_normalized"] == "smithjones.com"  # via normalize_url
    assert rec["phone_normalized"] == "+16025551234"  # E.164
    assert rec["name_normalized"]  # populated via normalize_firm_name
