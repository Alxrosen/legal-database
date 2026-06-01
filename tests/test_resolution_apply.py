"""Tests for the canonical-Firm apply step."""

from __future__ import annotations

from legal_sourcing.resolution.apply import _UnionFind


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
