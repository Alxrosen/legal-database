"""Resolution tests: blocking + scoring on synthetic in-memory records."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from legal_sourcing.resolution.blocking import (
    generate_candidate_pairs,
    make_blocking_keys,
)
from legal_sourcing.resolution.scoring import score_pair


def _rec(**kwargs):
    """Quick FirmSourceRecord-shaped namespace for blocking/scoring."""
    defaults = dict(
        id=1,
        name_raw=None,
        name_normalized=None,
        website_normalized=None,
        phone_normalized=None,
        primary_city=None,
        primary_state=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---- Blocking ----------------------------------------------------------


def test_make_blocking_keys_emits_phone_website_namestate():
    r = _rec(
        id=1,
        name_normalized="acme law",
        primary_state="AZ",
        phone_normalized="+16025551234",
        website_normalized="acme.law",
    )
    keys = make_blocking_keys(r)
    assert "phone:+16025551234" in keys
    assert "website:acme.law" in keys
    # name_state key uses 8-char prefix; "acme law" is 8 chars exactly.
    assert "name_state:acme law|AZ" in keys
    assert len(keys) == 3


def test_make_blocking_keys_handles_missing_fields():
    """No phone/website/state -> no keys (the record is unblockable)."""
    r = _rec(id=2, name_normalized="solo attorney")
    keys = make_blocking_keys(r)
    assert keys == set()


def test_make_blocking_keys_truncates_long_names():
    r = _rec(
        id=3,
        name_normalized="the husband wife law team",
        primary_state="AZ",
    )
    keys = make_blocking_keys(r)
    # First NAME_PREFIX_LEN chars + state.
    assert "name_state:the husb|AZ" in keys


def test_generate_candidate_pairs_dedupes_and_orders():
    """Two records sharing two keys must produce ONE (a_id<b_id) pair."""
    records = [
        _rec(
            id=10,
            name_normalized="acme law",
            primary_state="AZ",
            phone_normalized="+1",
            website_normalized="acme.law",
        ),
        _rec(
            id=11,
            name_normalized="acme law",
            primary_state="AZ",
            phone_normalized="+1",
            website_normalized="acme.law",
        ),
        _rec(id=12, name_normalized="other firm", primary_state="AZ"),
    ]
    pairs = list(generate_candidate_pairs(records))
    assert pairs == [(10, 11)]  # one pair, ordered


def test_generate_candidate_pairs_blocks_only_within_buckets():
    records = [
        _rec(id=20, phone_normalized="+1"),
        _rec(id=21, phone_normalized="+1"),
        _rec(id=22, phone_normalized="+2"),
        _rec(id=23, phone_normalized="+2"),
    ]
    pairs = sorted(generate_candidate_pairs(records))
    assert pairs == [(20, 21), (22, 23)]


# ---- Scoring -----------------------------------------------------------


def test_score_pair_identical_records_high():
    r = _rec(
        id=1,
        name_normalized="acme law llp",
        phone_normalized="+16025551234",
        website_normalized="acme.law",
        primary_city="Phoenix",
        primary_state="AZ",
        name_raw="Acme Law LLP",
    )
    s = score_pair(r, r)
    # All components present + matching -> at or near 100.
    assert s["total"] >= 95.0
    assert s["components"]["name_sim"] == pytest.approx(1.0)
    assert s["components"]["phone_exact"] == 1.0
    assert s["components"]["website_exact"] == 1.0
    assert s["components"]["city_match"] == 1.0
    assert s["components"]["state_match"] == 1.0


def test_score_pair_completely_different_low():
    a = _rec(
        id=1,
        name_normalized="acme law",
        phone_normalized="+16025551234",
        website_normalized="acme.law",
        primary_city="Phoenix",
        primary_state="AZ",
    )
    b = _rec(
        id=2,
        name_normalized="zenith partners",
        phone_normalized="+19999999999",
        website_normalized="zenith.com",
        primary_city="Mobile",
        primary_state="AL",
    )
    s = score_pair(a, b)
    # All distinct. State+city mismatch contribute 0. Name barely overlaps.
    assert s["total"] < 30.0


def test_score_pair_missing_signals_are_neutral():
    """When phone/website not on either side, the score is just
    name_sim * its weight + whatever else is present."""
    a = _rec(id=1, name_normalized="acme law")
    b = _rec(id=2, name_normalized="acme law")
    s = score_pair(a, b)
    # name_sim = 1.0, weight = 45 -> total ~45.
    assert s["components"]["phone_exact"] is None
    assert s["components"]["website_exact"] is None
    assert s["total"] == pytest.approx(45.0)


def test_score_pair_suffix_mismatch_penalty():
    """Same name + different entity suffix -> small penalty."""
    a = _rec(
        id=1,
        name_normalized="acme law",
        name_raw="Acme Law, LLP",
        phone_normalized="+1",
        website_normalized="acme.law",
        primary_city="Phoenix",
        primary_state="AZ",
    )
    b = _rec(
        id=2,
        name_normalized="acme law",
        name_raw="Acme Law, PC",
        phone_normalized="+1",
        website_normalized="acme.law",
        primary_city="Phoenix",
        primary_state="AZ",
    )
    s = score_pair(a, b)
    no_penalty_a = _rec(
        id=1,
        name_normalized="acme law",
        name_raw="Acme Law, LLP",
        phone_normalized="+1",
        website_normalized="acme.law",
        primary_city="Phoenix",
        primary_state="AZ",
    )
    no_penalty_b = _rec(
        id=2,
        name_normalized="acme law",
        name_raw="Acme Law, LLP",
        phone_normalized="+1",
        website_normalized="acme.law",
        primary_city="Phoenix",
        primary_state="AZ",
    )
    s_no_penalty = score_pair(no_penalty_a, no_penalty_b)
    assert s["total"] < s_no_penalty["total"]
    assert s["components"]["suffix_diff_penalty"] == -1.0


def test_score_pair_phone_match_dominates_name_diff():
    """Reasonable real-world case: same firm, slightly different rendered
    name (DBA vs LLC), but phone + website + city all match -> high
    confidence."""
    a = _rec(
        id=1,
        name_normalized="smith jones",
        phone_normalized="+16025551234",
        website_normalized="sj.law",
        primary_city="Phoenix",
        primary_state="AZ",
    )
    b = _rec(
        id=2,
        name_normalized="smith jones partners",
        phone_normalized="+16025551234",
        website_normalized="sj.law",
        primary_city="Phoenix",
        primary_state="AZ",
    )
    s = score_pair(a, b)
    assert s["total"] > 80.0


def test_score_pair_state_mismatch_caps_total():
    """Same firm name, different state — should land in the rejected
    band: name still matches, but city/state contradict."""
    a = _rec(id=1, name_normalized="morgan morgan", primary_city="Phoenix", primary_state="AZ")
    b = _rec(id=2, name_normalized="morgan morgan", primary_city="Mobile", primary_state="AL")
    s = score_pair(a, b)
    # name_sim=1*45 + city_match=0*5 + state_match=0*5 = 45.
    assert s["total"] == pytest.approx(45.0)


# ---- strong-identifier floors / caps -----------------------------------


def test_website_identity_match_floors_despite_phone_diff():
    """Same identity domain + same name but DIFFERENT office phones -> auto."""
    a = _rec(
        id=1,
        name_normalized="acme law",
        name_raw="Acme Law LLP",
        website_normalized="acme.law",
        phone_normalized="+16020000001",
    )
    b = _rec(
        id=2,
        name_normalized="acme law",
        name_raw="Acme Law LLP",
        website_normalized="acme.law",
        phone_normalized="+16020000002",
    )
    assert score_pair(a, b)["total"] >= 85.0


def test_aggregator_website_is_not_a_merge_signal():
    """Two records that both list facebook.com are NOT thereby a match."""
    a = _rec(id=1, website_normalized="facebook.com")
    b = _rec(id=2, website_normalized="facebook.com")
    s = score_pair(a, b)
    assert s["components"]["website_exact"] is None
    assert s["total"] < 60.0


def test_phone_plus_strong_name_floors():
    """Same phone + same name, no website -> auto (websiteless firms)."""
    a = _rec(
        id=1,
        name_normalized="smith jones",
        name_raw="Smith Jones LLP",
        phone_normalized="+16025551234",
    )
    b = _rec(
        id=2,
        name_normalized="smith jones",
        name_raw="Smith Jones LLP",
        phone_normalized="+16025551234",
    )
    assert score_pair(a, b)["total"] >= 85.0


def test_name_plus_location_floors():
    """Same name + same city + same state, no phone/website -> auto."""
    a = _rec(
        id=1,
        name_normalized="smith jones",
        name_raw="Smith Jones LLP",
        primary_city="Phoenix",
        primary_state="AZ",
    )
    b = _rec(
        id=2,
        name_normalized="smith jones",
        name_raw="Smith Jones LLP",
        primary_city="Phoenix",
        primary_state="AZ",
    )
    assert score_pair(a, b)["total"] >= 85.0


def test_website_conflict_caps_phone_and_name_match():
    """Same name + same phone but DIFFERENT identity websites = different firms."""
    a = _rec(
        id=1,
        name_normalized="acme law",
        name_raw="Acme Law LLP",
        phone_normalized="+16025551234",
        website_normalized="acme.law",
    )
    b = _rec(
        id=2,
        name_normalized="acme law",
        name_raw="Acme Law LLP",
        phone_normalized="+16025551234",
        website_normalized="acmelaw.net",
    )
    assert score_pair(a, b)["total"] < 85.0


def test_name_conflict_caps_website_match():
    """Two clearly-different firm names sharing one domain are not auto-merged."""
    a = _rec(
        id=1, name_normalized="acme law", name_raw="Acme Law LLP", website_normalized="shared.com"
    )
    b = _rec(
        id=2,
        name_normalized="zenith partners",
        name_raw="Zenith Partners LLP",
        website_normalized="shared.com",
    )
    assert score_pair(a, b)["total"] < 85.0
