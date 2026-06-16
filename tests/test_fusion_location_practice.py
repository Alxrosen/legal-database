"""Tests for fusing location (city/state) + practice areas onto the canonical firm.

Pure-function tests (no DB / no network): build FirmSourceRecord objects and assert
`fuse_cluster` fills `city`, `state`, `practice_areas` as the UNION of ALL distinct
offices / practice areas across the cluster (not just the dominant office).
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


def test_fuse_cluster_unions_all_offices_and_practices():
    members = [
        _fsr(
            primary_city="Phoenix", primary_state="AZ", practice_areas_matched=["personal-injury"]
        ),
        _fsr(primary_city="Phoenix", primary_state="AZ", practice_areas_matched=["family-law"]),
        _fsr(primary_city="Tucson", primary_state="AZ", practice_areas_matched=[]),
    ]
    res = fuse_cluster(members, now=_TS)
    # ALL distinct offices (sorted), not just the dominant one.
    assert res.city == ["Phoenix", "Tucson"]
    assert res.state == ["AZ"]
    # Practice areas are the UNION across the cluster.
    assert set(res.practice_areas) == {"personal-injury", "family-law"}
    assert res.field_provenance["city"]["method"] == "union"
    assert res.field_provenance["practice_areas"]["method"] == "union"


def test_fuse_cluster_all_states_and_filters_address_garbage_cities():
    members = [
        _fsr(primary_city="Phoenix", primary_state="AZ", practice_areas_matched=[]),
        _fsr(primary_city="Los Angeles", primary_state="CA", practice_areas_matched=[]),
        # Mis-parsed address in the city field (has digits) -> filtered out; its
        # state still counts toward the union.
        _fsr(
            primary_city="555 Bluff St, St George UT 84770",
            primary_state="UT",
            practice_areas_matched=[],
        ),
    ]
    res = fuse_cluster(members, now=_TS)
    assert res.state == ["AZ", "CA", "UT"]
    assert res.city == ["Los Angeles", "Phoenix"]  # garbage city dropped
    assert res.practice_areas is None


def test_fuse_cluster_no_location():
    res = fuse_cluster([_fsr(primary_city=None, primary_state=None)], now=_TS)
    assert res.city is None
    assert res.state is None


def test_fuse_practice_areas_includes_website_enrichment():
    enr = SimpleNamespace(practice_areas=["estate-planning"])
    members = [_fsr(practice_areas_matched=["family-law"])]
    assert fuse_practice_areas(members, enr) == ["estate-planning", "family-law"]
    assert fuse_practice_areas(members, None) == ["family-law"]
    assert fuse_practice_areas([_fsr(practice_areas_matched=[])], None) is None
