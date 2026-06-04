"""Tests for the truth-discovery fusion (resolution/fusion.py) and the
resolution identity helpers (resolution/identity.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from legal_sourcing.resolution.fusion import (
    fuse_attorney_count,
    fuse_cluster,
    fuse_name,
    fuse_website,
)
from legal_sourcing.resolution.identity import is_firm_name, is_identity_website

NOW = datetime(2026, 6, 4, tzinfo=UTC)


def _rec(
    id=1,
    source="az_bar",
    name_raw=None,
    name_normalized=None,
    phone_normalized=None,
    phone_raw=None,
    website_normalized=None,
    website_raw=None,
    year_founded=None,
    attorney_count=None,
    contacts=None,
    scraped_at=None,
    enrichment_status=None,
):
    return SimpleNamespace(
        id=id,
        source=source,
        name_raw=name_raw,
        name_normalized=name_normalized,
        phone_normalized=phone_normalized,
        phone_raw=phone_raw,
        website_normalized=website_normalized,
        website_raw=website_raw,
        year_founded=year_founded,
        attorney_count=attorney_count,
        contacts=contacts or [],
        scraped_at=scraped_at,
        enrichment_status=enrichment_status,
    )


def _enr(**kw):
    base = dict(
        website="swlaw.com",
        url_verification_status="verified",
        attorney_count_min=None,
        attorney_count_is_min=False,
        attorney_count_raw=None,
        years_in_operation_min=None,
        years_is_min=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ---- identity helpers ------------------------------------------------------


def test_is_firm_name_fixes_pa_overmatch():
    # The shared looks_like_firm matched " pa" inside these surnames; the fix
    # must NOT classify them as firms.
    assert not is_firm_name("Amy Carol Parker")
    assert not is_firm_name("Patrick Lee")
    assert not is_firm_name("Madison Parks")
    assert not is_firm_name("Arizona Supreme Court")
    assert not is_firm_name(None)
    # True positives still hold.
    assert is_firm_name("Smith Law")
    assert is_firm_name("Snell & Wilmer LLP")
    assert is_firm_name("Morgan & Morgan")
    assert is_firm_name("Doe Law Offices")
    assert is_firm_name("Smith PA")  # standalone entity-suffix token
    assert is_firm_name("Jones P.C.")


def test_is_identity_website():
    assert is_identity_website("swlaw.com")
    assert is_identity_website("smith-jones.law")
    assert not is_identity_website("facebook.com")  # aggregator/social
    assert not is_identity_website("avvo.com")
    assert not is_identity_website("weebly.com")  # website-builder platform
    assert not is_identity_website("firmname.wixsite.com")  # subdomain of platform
    assert not is_identity_website(None)
    assert not is_identity_website("")


# ---- name fusion -----------------------------------------------------------


def test_fuse_name_picks_most_frequent_firm_variant():
    members = [
        _rec(id=1, name_raw="Snell & Wilmer LLP", name_normalized="snell wilmer"),
        _rec(id=2, name_raw="Snell & Wilmer LLP", name_normalized="snell wilmer"),
        _rec(id=3, name_raw="Snell & Wilmer LLP", name_normalized="snell wilmer"),
        _rec(id=4, name_raw="Snell & Wilmer L.L.P.", name_normalized="snell wilmer l l p"),
        _rec(id=5, source="justia", name_raw="", name_normalized=None),  # no name
        _rec(id=6, name_raw="Arizona Supreme Court", name_normalized="arizona supreme court"),
    ]
    choice = fuse_name(members, NOW)
    assert choice is not None
    assert choice.value == "Snell & Wilmer LLP"
    assert choice.method == "weighted_vote"


def test_fuse_name_gates_out_person_names():
    # FindLaw lists individual attorneys with the firm's identity; the firm
    # name must win over the person cards.
    members = [
        _rec(id=1, source="findlaw", name_raw="Morgan & Morgan", name_normalized="morgan morgan"),
        _rec(id=2, source="findlaw", name_raw="Morgan & Morgan", name_normalized="morgan morgan"),
        _rec(id=3, source="findlaw", name_raw="Ike Gulas", name_normalized="ike gulas"),
        _rec(
            id=4, source="findlaw", name_raw="Amy Carol Parker", name_normalized="amy carol parker"
        ),
    ]
    choice = fuse_name(members, NOW)
    assert choice is not None
    assert choice.value == "Morgan & Morgan"


def test_fuse_name_person_fallback_when_no_firm_name():
    members = [
        _rec(id=1, source="justia", name_raw="Jane A. Roe", name_normalized="jane a roe"),
    ]
    choice = fuse_name(members, NOW)
    assert choice is not None
    assert choice.value == "Jane A. Roe"
    assert choice.method == "person_fallback"


def test_fuse_name_none_when_all_empty():
    members = [_rec(id=1, source="justia", name_raw="", name_normalized=None)]
    assert fuse_name(members, NOW) is None


# ---- attorney-count fusion -------------------------------------------------


def test_attorney_count_verified_website_wins():
    members = [
        _rec(id=1, contacts=[{"name_normalized": "a"}]),
        _rec(id=2, contacts=[{"name_normalized": "b"}]),
    ]  # union would be 2
    enr = _enr(
        attorney_count_min=500, attorney_count_is_min=True, url_verification_status="verified"
    )
    choice = fuse_attorney_count(members, enr, NOW)
    assert choice is not None
    assert choice.value == 500
    assert choice.method == "website_verified"
    assert choice.extra["is_min"] is True


def test_attorney_count_ignores_unverified_website():
    members = [
        _rec(id=1, contacts=[{"name_normalized": "a"}]),
        _rec(id=2, contacts=[{"name_normalized": "b"}]),
    ]
    enr = _enr(attorney_count_min=500, url_verification_status="legal_but_mismatched")
    choice = fuse_attorney_count(members, enr, NOW)
    assert choice is not None
    assert choice.value == 2  # falls back to the distinct union
    assert choice.method == "distinct_union"
    assert choice.extra["is_min"] is True


def test_attorney_count_union_dedups_cross_source_by_name():
    members = [
        _rec(id=1, source="az_bar", contacts=[{"name_normalized": "john smith"}]),
        _rec(id=2, source="justia", contacts=[{"name_normalized": "john smith"}]),
        _rec(id=3, source="justia", contacts=[{"name_normalized": "jane doe"}]),
    ]
    choice = fuse_attorney_count(members, None, NOW)
    assert choice is not None
    assert choice.value == 2


# ---- website fusion --------------------------------------------------------


def test_fuse_website_excludes_generic_domains():
    members = [
        _rec(id=1, website_normalized="facebook.com", website_raw="https://facebook.com/x"),
        _rec(id=2, website_normalized="smithlaw.com", website_raw="https://smithlaw.com"),
        _rec(id=3, website_normalized="smithlaw.com", website_raw="https://smithlaw.com"),
    ]
    choice = fuse_website(members, NOW)
    assert choice is not None
    assert choice.value == "smithlaw.com"


# ---- end-to-end cluster fusion ---------------------------------------------


def test_fuse_cluster_snell_wilmer_like():
    members = [
        _rec(
            id=1,
            source="az_bar",
            name_raw="Snell & Wilmer LLP",
            name_normalized="snell wilmer",
            phone_normalized="+16023826000",
            phone_raw="(602) 382-6000",
            website_normalized="swlaw.com",
            website_raw="https://www.swlaw.com",
            contacts=[{"name_normalized": "lawyer one"}],
        ),
        _rec(
            id=2,
            source="justia",
            name_raw="",
            name_normalized=None,
            phone_normalized="+17027845200",
            website_normalized="swlaw.com",
            contacts=[{"name_normalized": "lawyer two"}],
        ),
        _rec(
            id=3,
            source="az_bar",
            name_raw="Arizona Supreme Court",  # outlier sharing the domain
            name_normalized="arizona supreme court",
            website_normalized="swlaw.com",
        ),
    ]
    enr = _enr(
        website="swlaw.com",
        attorney_count_min=500,
        attorney_count_is_min=True,
        url_verification_status="verified",
        years_in_operation_min=88,
    )
    result = fuse_cluster(members, enr, now=NOW)
    assert result.name == "Snell & Wilmer LLP"  # outlier + empty name lose
    assert result.website_normalized == "swlaw.com"
    assert result.attorney_count == 500
    assert result.year_founded == NOW.year - 88
    # provenance recorded for each chosen field
    assert set(result.field_provenance) >= {"name", "website", "attorney_count"}
    assert result.field_provenance["attorney_count"]["method"] == "website_verified"
    assert "attorney_count is a lower bound" in " ".join(result.notes)
