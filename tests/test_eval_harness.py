"""Tests for the engine-agnostic resolution eval harness (pure logic).

The DB-backed ``build_eval_set`` is exercised by running the CLI against the
live shared DB; here we unit-test the correctness-critical pure functions:
labeling, B-cubed, the pairwise sweep, and the blocking-gated score adapter.
"""

from __future__ import annotations

from types import SimpleNamespace

from legal_sourcing.resolution.eval_harness import (
    EvalSet,
    LabeledPair,
    _domain_to_oracle_firm,
    _load_clerical_labels,
    _multidomain_same_firm,
    _ordered,
    bcubed,
    bespoke_score_fn,
    identity_domain,
    pairwise_sweep,
    true_firm_id,
)


def _rec(**kw):
    defaults = dict(
        id=1,
        name_raw=None,
        name_normalized=None,
        website_normalized=None,
        phone_normalized=None,
        primary_city=None,
        primary_state=None,
        source="justia",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ---- labeling ----------------------------------------------------------


def test_identity_domain_only_for_identity_websites():
    assert identity_domain(_rec(website_normalized="acmelaw.com")) == "acmelaw.com"
    # aggregator / platform domains are not identity -> not labelable
    assert identity_domain(_rec(website_normalized="facebook.com")) is None
    assert identity_domain(_rec(website_normalized="")) is None
    assert identity_domain(_rec(website_normalized=None)) is None


def test_true_firm_id_oracle_merges_multi_domain():
    d2f = _domain_to_oracle_firm()
    # Two DISTINCT Dickinson Wright domains must map to the SAME firm id.
    a = true_firm_id(_rec(website_normalized="dickinson-wright.com"), d2f)
    b = true_firm_id(_rec(website_normalized="dickinsonwright.com"), d2f)
    assert a == b == "oracle:dickinson_wright"


def test_true_firm_id_non_oracle_uses_domain():
    d2f = _domain_to_oracle_firm()
    assert true_firm_id(_rec(website_normalized="acmelaw.com"), d2f) == "web:acmelaw.com"
    # no identity website -> un-labelable
    assert true_firm_id(_rec(website_normalized=None), d2f) is None


def test_ordered():
    assert _ordered(5, 2) == (2, 5)
    assert _ordered(2, 5) == (2, 5)


# ---- B-cubed -----------------------------------------------------------


def test_bcubed_perfect_clustering():
    truth = {1: "A", 2: "A", 3: "B"}
    pred = {1: "x", 2: "x", 3: "y"}  # same partition, different labels
    p, r, f1 = bcubed(truth, pred)
    assert (p, r, f1) == (1.0, 1.0, 1.0)


def test_bcubed_over_merge_hurts_precision_not_recall():
    truth = {1: "A", 2: "A", 3: "B"}
    pred = {1: "x", 2: "x", 3: "x"}  # everything lumped together
    p, r, f1 = bcubed(truth, pred)
    assert r == 1.0  # no same-firm pair was split
    assert p < 1.0  # but firm B got polluted
    assert 0.0 < f1 < 1.0


def test_bcubed_under_merge_hurts_recall_not_precision():
    truth = {1: "A", 2: "A", 3: "A"}
    pred = {1: "x", 2: "y", 3: "z"}  # all split apart
    p, r, _f1 = bcubed(truth, pred)
    assert p == 1.0  # no cluster mixes two firms
    assert r < 1.0


# ---- pairwise sweep ----------------------------------------------------


def test_pairwise_sweep_counts_and_metrics():
    pairs = [
        LabeledPair(1, 2, 1, "candidate_pos"),  # match, scores high
        LabeledPair(3, 4, 1, "candidate_pos"),  # match, scores low (recall miss)
        LabeledPair(5, 6, 0, "candidate_neg"),  # non-match, scores high (false pos)
        LabeledPair(7, 8, 0, "candidate_neg"),  # non-match, scores low (true neg)
    ]
    scores = {(1, 2): 90.0, (3, 4): 10.0, (5, 6): 90.0, (7, 8): 10.0}
    es = EvalSet(pairs=pairs, truth_by_id={}, records={}, candidate_keys=set())
    [pt] = pairwise_sweep(es, lambda a, b: scores[(a, b)], [50.0])
    assert (pt.tp, pt.fp, pt.fn, pt.tn) == (1, 1, 1, 1)
    assert pt.precision == 0.5
    assert pt.recall == 0.5
    assert pt.f1 == 0.5


# ---- blocking-gated bespoke adapter ------------------------------------


def test_bespoke_score_fn_zeros_unblocked_pairs():
    a = _rec(id=1, website_normalized="acmelaw.com", name_normalized="acme law")
    b = _rec(id=2, website_normalized="acmelaw.com", name_normalized="acme law")
    es = EvalSet(
        pairs=[],
        truth_by_id={},
        records={1: a, 2: b},
        candidate_keys={(1, 2)},  # only this pair is "proposed"
    )
    fn = bespoke_score_fn(es)
    assert fn(1, 2) > 0  # blocked + website-identity match -> high
    # a pair NOT in candidate_keys must score 0 (counts as a recall miss)
    es2 = EvalSet(pairs=[], truth_by_id={}, records={1: a, 2: b}, candidate_keys=set())
    assert bespoke_score_fn(es2)(1, 2) == 0.0


# ---- clerical labels ---------------------------------------------------


def test_load_clerical_labels_parses_and_orders(tmp_path):
    p = tmp_path / "labels.csv"
    p.write_text(
        "a_id,b_id,label,note\n50,10,1,attorney->firm\n7,9,0,different\nbad,row,x,skip\n",
        encoding="utf-8",
    )
    rows = _load_clerical_labels(str(p))
    assert (10, 50, 1) in rows  # ids reordered a<b
    assert (7, 9, 0) in rows
    assert len(rows) == 2  # the malformed row is dropped


def test_load_clerical_labels_missing_file():
    assert _load_clerical_labels("does/not/exist.csv") == []


# ---- multi-domain de-bias ----------------------------------------------


def test_multidomain_same_firm_shared_phone_and_name():
    # Same firm, two domains, same phone -> de-bias to a positive.
    a = _rec(name_normalized="frank t waters law", phone_normalized="+19284355047")
    b = _rec(name_normalized="law offices of frank t waters", phone_normalized="+19284355047")
    assert _multidomain_same_firm(a, b) is True


def test_multidomain_same_firm_rejects_leadgen():
    # Shared phone but DIFFERENT names == lead-gen / different firms -> stays negative.
    a = _rec(name_normalized="smith injury law", phone_normalized="+17623800028")
    b = _rec(name_normalized="jones bankruptcy group", phone_normalized="+17623800028")
    assert _multidomain_same_firm(a, b) is False


def test_multidomain_same_firm_requires_shared_phone():
    a = _rec(name_normalized="acme law", phone_normalized="+16025551234")
    b = _rec(name_normalized="acme law", phone_normalized="+16025559999")
    assert _multidomain_same_firm(a, b) is False
