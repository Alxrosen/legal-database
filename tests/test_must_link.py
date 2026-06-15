"""Tests for the website MUST-LINK recall pass (resolution/must_link.py).

Synthetic frames mirror what `splink_linker.extract_frame` produces (the columns
the pass reads: unique_id, website_identity, name_core_key, source,
name_normalized, phone_normalized). Each test pins one behaviour:

* a real false negative merges (acronym website record + full-name az_bar record)
* a mis-attributed website is excluded (conflicting phone; or "owns another home")
* a generic/platform/gov domain is skipped (Gate A)
* a cross-domain redirect merges the acquired firm with the acquirer
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from legal_sourcing.models import WebsiteEnrichment
from legal_sourcing.models.base import Base
from legal_sourcing.resolution.must_link import (
    _name_domain_affinity,
    build_redirect_map,
    website_must_link_edges,
)

_COLS = (
    "unique_id",
    "website_identity",
    "name_core_key",
    "source",
    "name_normalized",
    "phone_normalized",
)


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{c: r.get(c) for c in _COLS} for r in rows])


def _norm(edges):
    return {tuple(sorted(e)) for e in edges}


def test_acronym_false_negative_merges():
    # hbsslaw.com: full-name az_bar record + "HBSS" website record -> same firm.
    df = _df(
        [
            {
                "unique_id": 1,
                "website_identity": "hbsslaw.com",
                "name_core_key": "hagens ber",
                "source": "az_bar",
                "name_normalized": "hagens berman sobol shapiro llp",
                "phone_normalized": "+16025551111",
            },
            {
                "unique_id": 2,
                "website_identity": "hbsslaw.com",
                "name_core_key": "hbss",
                "source": "website",
                "name_normalized": "hbss",
                "phone_normalized": None,
            },
        ]
    )
    edges, stats, groups = website_must_link_edges(df, {})
    assert _norm(edges) == {(1, 2)}
    assert groups["hbsslaw.com"]["excluded"] == []
    assert stats["domains_linked"] == 1


def test_misattributed_website_excluded_by_phone_conflict():
    # zellaw.com belongs to Zelms Erlich Lenkov; Maxwell & Morgan wrongly carries it
    # (different phone) -> Maxwell excluded, Zelms records + website merge.
    df = _df(
        [
            {
                "unique_id": 1,
                "website_identity": "zellaw.com",
                "name_core_key": "zelms erli",
                "source": "az_bar",
                "name_normalized": "zelms erlich lenkov llp",
                "phone_normalized": "+13105550001",
            },
            {
                "unique_id": 2,
                "website_identity": "zellaw.com",
                "name_core_key": "zelms erli",
                "source": "az_bar",
                "name_normalized": "zelms erlich lenkov",
                "phone_normalized": "+13105550001",
            },
            {
                "unique_id": 3,
                "website_identity": "zellaw.com",
                "name_core_key": "zelms",
                "source": "website",
                "name_normalized": "zelms erlich lenkov",
                "phone_normalized": None,
            },
            {
                "unique_id": 4,
                "website_identity": "zellaw.com",
                "name_core_key": "maxwell mo",
                "source": "az_bar",
                "name_normalized": "maxwell & morgan pc",
                "phone_normalized": "+14808331001",
            },
        ]
    )
    edges, stats, groups = website_must_link_edges(df, {})
    assert 4 in groups["zellaw.com"]["excluded"]
    assert _norm(edges) == {(1, 2), (1, 3)}  # Zelms + website, NOT Maxwell
    assert stats["misattribution_excluded"] == 1


def test_misattributed_website_excluded_no_affinity():
    # rlb.com belongs to Rider Levett Bucknall (initials RLB); GlassRatner's name
    # has no affinity with "rlb" and no shared phone -> excluded as a mis-attribution.
    df = _df(
        [
            {
                "unique_id": 1,
                "website_identity": "rlb.com",
                "name_core_key": "rider lev",
                "source": "website",
                "name_normalized": "rider levett bucknall ltd",
                "phone_normalized": None,
            },
            {
                "unique_id": 2,
                "website_identity": "rlb.com",
                "name_core_key": "glassratne",
                "source": "az_bar",
                "name_normalized": "glassratner advisory group",
                "phone_normalized": None,
            },
        ]
    )
    edges, _, groups = website_must_link_edges(df, {})
    assert 2 in groups["rlb.com"]["excluded"]
    assert _norm(edges) == set()  # only Rider Levett left on rlb.com (a singleton -> no edge)


def test_name_domain_affinity():
    # A firm's own (acronym / token / stub) domain has affinity; a mis-attributed
    # one does not.
    assert _name_domain_affinity("hagens berman sobol shapiro llp", "hbsslaw.com")  # initials
    assert _name_domain_affinity("koeller nebeker carlson & haluck llp", "knchlaw.com")  # initials
    assert _name_domain_affinity("sherman & howard llc", "shermanhoward.com")  # token
    assert _name_domain_affinity("lincoln & wenk pllc", "lwazlaw.com")  # initials lw(az)
    assert not _name_domain_affinity("maxwell & morgan pc", "zellaw.com")  # mis-attribution
    assert not _name_domain_affinity("glassratner advisory group", "rlb.com")  # mis-attribution


def test_generic_domain_skipped_gate_a():
    rows = [
        {
            "unique_id": i,
            "website_identity": "azbar.org",
            "name_core_key": f"firm{i:02d}",
            "source": "az_bar",
            "name_normalized": f"firm {i} llp",
            "phone_normalized": None,
        }
        for i in range(1, 7)  # 6 distinct cores > generic_min_distinct (5)
    ]
    edges, stats, groups = website_must_link_edges(_df(rows), {})
    assert edges == []
    assert stats.get("generic_domains_skipped") == 1
    assert "azbar.org" not in groups


def test_cross_domain_redirect_merges_acquired_firm():
    # shermanhoward.com redirects to taftlaw.com (acquisition) -> one firm.
    df = _df(
        [
            {
                "unique_id": 1,
                "website_identity": "shermanhoward.com",
                "name_core_key": "sherman ho",
                "source": "az_bar",
                "name_normalized": "sherman & howard llc",
                "phone_normalized": "+13035550000",
            },
            {
                "unique_id": 2,
                "website_identity": "taftlaw.com",
                "name_core_key": "taft stet",
                "source": "website",
                "name_normalized": "taft stettinius & hollister llp",
                "phone_normalized": None,
            },
        ]
    )
    edges, _, groups = website_must_link_edges(df, {"shermanhoward.com": "taftlaw.com"})
    assert _norm(edges) == {(1, 2)}
    assert "taftlaw.com" in groups and "shermanhoward.com" not in groups


def test_build_redirect_map(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'we.sqlite'}")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add_all(
            [
                WebsiteEnrichment(
                    website="shermanhoward.com", resolved_url="https://www.taftlaw.com/"
                ),
                WebsiteEnrichment(
                    website="smithlaw.com", resolved_url="https://smithlaw.com/home"
                ),  # self
                WebsiteEnrichment(website="dead.com", resolved_url=None),  # no target
                WebsiteEnrichment(
                    website="builton.com", resolved_url="https://builton.wixsite.com/x"
                ),  # platform
            ]
        )
        s.commit()
        rmap = build_redirect_map(s)
    assert rmap == {"shermanhoward.com": "taftlaw.com"}
