"""Tests for fusing location (city/state) + practice areas onto the canonical firm.

Pure-function tests (no DB / no network): build FirmSourceRecord objects and assert
`fuse_cluster` fills `city`, `state`, `practice_areas` (and that location is a
consistent (city, state) pair chosen by weighted vote — the dominant office).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from legal_sourcing.models import FirmSourceRecord
from legal_sourcing.resolution.fusion import fuse_cluster, fuse_practice_areas

_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _fsr(**kw) -> FirmSourceRecord:
    base = dict(source="az_bar", source_url="https://x", scraped_at=_TS, name_raw="Acme Law LLP")
    base.update(kw)
    return FirmSourceRecord(**base)


def test_fuse_cluster_picks_dominant_office_and_unions_practices():
    members = [
        _fsr(
            primary_city="Phoenix", primary_state="AZ", practice_areas_matched=["personal-injury"]
        ),
        _fsr(primary_city="Phoenix", primary_state="AZ", practice_areas_matched=["family-law"]),
        _fsr(primary_city="Tucson", primary_state="AZ", practice_areas_matched=[]),
    ]
    res = fuse_cluster(members, now=_TS)
    # Dominant (most-supported) office is Phoenix, AZ; city + state stay consistent.
    assert res.city == "Phoenix"
    assert res.state == "AZ"
    # Practice areas are the UNION across the cluster.
    assert set(res.practice_areas) == {"personal-injury", "family-law"}
    assert "location" in res.field_provenance
    assert res.field_provenance["practice_areas"]["method"] == "union"


def test_fuse_cluster_location_requires_state():
    # No state anywhere -> city/state stay None (state is the anchor).
    members = [_fsr(primary_city="Nowhere", primary_state=None, practice_areas_matched=[])]
    res = fuse_cluster(members, now=_TS)
    assert res.city is None
    assert res.state is None
    assert res.practice_areas is None


def test_fuse_practice_areas_includes_website_enrichment():
    enr = SimpleNamespace(practice_areas=["estate-planning"])
    members = [_fsr(practice_areas_matched=["family-law"])]
    assert fuse_practice_areas(members, enr) == ["estate-planning", "family-law"]
    assert fuse_practice_areas(members, None) == ["family-law"]
    assert fuse_practice_areas([_fsr(practice_areas_matched=[])], None) is None
