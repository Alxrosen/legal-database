"""Tests for the canonical-Firm apply step."""

from __future__ import annotations

from types import SimpleNamespace

from legal_sourcing.resolution.apply import (
    _aggregate_attorney_count,
    _looks_like_firm,
    _UnionFind,
)


def _rec(source, contacts=None, name_raw=None, name_normalized=None):
    """Minimal stand-in for a FirmSourceRecord (apply reads only these)."""
    return SimpleNamespace(
        source=source,
        contacts=contacts or [],
        name_raw=name_raw,
        name_normalized=name_normalized,
    )


# ---- distinct-attorney count (email -> name -> id dedup) ------------------


def test_attorney_count_distinct_with_cross_source_name_dedup():
    members = [
        _rec("az_bar", contacts=[{"name_normalized": "john smith", "entity_number": "1"}]),
        # same person in justia -> name dedups across sources
        _rec("justia", contacts=[{"name_normalized": "john smith", "source_attorney_id": "99"}]),
        _rec("justia", contacts=[{"name_normalized": "jane doe", "source_attorney_id": "100"}]),
    ]
    assert _aggregate_attorney_count(members) == 2


def test_attorney_count_email_dedups_name_variants():
    members = [
        _rec(
            "az_bar",
            contacts=[{"name_normalized": "robert j smith", "email_normalized": "r@firm.com"}],
        ),
        _rec(
            "martindale",
            contacts=[{"name_normalized": "bob smith", "email_normalized": "r@firm.com"}],
        ),
    ]
    assert _aggregate_attorney_count(members) == 1


def test_attorney_count_findlaw_person_card_counts_firm_card_does_not():
    members = [
        _rec("findlaw", contacts=[], name_raw="Bob Lee", name_normalized="bob lee"),
        _rec("findlaw", contacts=[], name_raw="Acme Law LLP", name_normalized="acme law llp"),
    ]
    assert _aggregate_attorney_count(members) == 1  # the firm card contributes 0


def test_attorney_count_none_when_no_attorneys():
    assert (
        _aggregate_attorney_count([_rec("findlaw", contacts=[], name_raw="Smith & Jones LLP")])
        is None
    )


def test_looks_like_firm():
    assert _looks_like_firm("Kutak Rock LLP")
    assert _looks_like_firm("Morgan & Morgan")
    assert _looks_like_firm("Doe Law Offices")
    assert not _looks_like_firm("A. Daniel Vazquez")
    assert not _looks_like_firm(None)


def test_unionfind_singletons():
    uf = _UnionFind()
    for k in (1, 2, 3):
        uf.find(k)
    comps = uf.components([1, 2, 3])
    assert len(comps) == 3
    assert sorted(sorted(v) for v in comps.values()) == [[1], [2], [3]]


def test_unionfind_simple_union():
    uf = _UnionFind()
    uf.union(1, 2)
    uf.union(3, 4)
    uf.union(2, 3)  # links the two pairs into one component
    comps = uf.components([1, 2, 3, 4, 5])
    sizes = sorted(len(v) for v in comps.values())
    assert sizes == [1, 4]  # one singleton (5), one quadruple


def test_unionfind_transitive_closure():
    uf = _UnionFind()
    uf.union(10, 20)
    uf.union(20, 30)
    uf.union(40, 50)
    comps = uf.components([10, 20, 30, 40, 50])
    sizes = sorted(len(v) for v in comps.values())
    assert sizes == [2, 3]


def test_unionfind_path_compression_idempotent():
    uf = _UnionFind()
    uf.union(1, 2)
    uf.union(2, 3)
    uf.union(3, 4)
    r1 = uf.find(1)
    r4 = uf.find(4)
    assert r1 == r4
    # second find call should also return same root (path-compression
    # doesn't corrupt the data).
    assert uf.find(1) == r1
