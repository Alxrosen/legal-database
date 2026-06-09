"""Splink-on-DuckDB linker — the pivot's matching+clustering engine.

Replaces the hand-rolled trio (``blocking.py`` keys + ``scoring.py`` weights/
floors/caps + ``apply.py``'s ``_UnionFind``) with Fellegi-Sunter and
**unsupervised EM-learned** m/u weights, per ``docs/audit/splink-adoption-plan.md``.
What STAYS: ``fusion.py`` (survivorship), ``identity.py`` (the
``is_identity_website`` pre-filter feeding ``website_identity``).

This module is import-isolated so the rest of resolution doesn't depend on
splink/duckdb/pandas. Its CLI runs the **head-to-head** the pivot is gated on:
Splink and the bespoke matcher scored on the IDENTICAL labeled eval set
(``eval_harness.build_eval_set``), so adopt-iff-it-beats is measured.

    uv run python -m legal_sourcing.resolution.splink_linker compare

Splink reads a DataFrame extract of the FSR identity columns (it does NOT need
Postgres; DuckDB is embedded), so it's decoupled from the operational store.
"""

from __future__ import annotations

import argparse
import logging
import sys

import pandas as pd
import splink.comparison_library as cl
from splink import DuckDBAPI, Linker, SettingsCreator, block_on
from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_sourcing.db import make_engine
from legal_sourcing.models import FirmSourceRecord
from legal_sourcing.resolution.identity import is_identity_website

# Splink/py4j-style chatter is noisy; quiet it for CLI runs.
logging.getLogger("splink").setLevel(logging.WARNING)

# Calibrated prior: P(two random *blocked-candidate* records are the same firm).
# Splink's deterministic estimate undershoots (~1e-5, treating the whole corpus as
# the random-pair space); within blocked candidates the true match rate is ~2e-3.
# At this lambda a near-unique-identifier match becomes decisive (the learned-model
# equivalent of the bespoke website floor) WITHOUT a hand-set floor -- sweeping it
# (CLI `prior`) showed it is the dominant accuracy lever (B-cubed 0.95 -> 0.98).
DEFAULT_PROB_TWO_RANDOM = 2e-3
# Seed for estimate_u_using_random_sampling so runs are reproducible (without it
# u wobbles run-to-run, e.g. pairwise F1 0.962 vs 0.994 at the same lambda).
_U_SEED = 20260608


# ---------------------------------------------------------------------------
# Extract (the only DB read; one row per FSR identity record)
# ---------------------------------------------------------------------------


def extract_frame(session: Session, ids: list[int] | None = None) -> pd.DataFrame:
    """Pull the FSR identity columns into a DataFrame for Splink.

    ``website_identity`` is the bare domain ONLY when ``is_identity_website``
    (else NULL) — the same pre-filter the bespoke blocking uses, so platform /
    aggregator domains can't block unrelated firms together.
    """
    cols = (
        FirmSourceRecord.id,
        FirmSourceRecord.name_normalized,
        FirmSourceRecord.phone_normalized,
        FirmSourceRecord.website_normalized,
        FirmSourceRecord.primary_city,
        FirmSourceRecord.primary_state,
        FirmSourceRecord.source,
    )
    if ids is None:
        rows = session.execute(select(*cols)).all()
    else:
        # Chunk the IN clause — SQLite caps bound variables (~32k).
        rows = []
        for i in range(0, len(ids), 900):
            chunk = ids[i : i + 900]
            rows.extend(session.execute(select(*cols).where(FirmSourceRecord.id.in_(chunk))).all())
    recs = []
    for id_, name, phone, web, city, state, source in rows:
        dom = (web or "").strip().lower() or None
        recs.append(
            {
                "unique_id": id_,
                "name_normalized": (name or "").strip().lower() or None,
                "phone_normalized": (phone or "").strip() or None,
                "website_identity": dom if (dom and is_identity_website(dom)) else None,
                "primary_city": (city or "").strip().lower() or None,
                "primary_state": (state or "").strip().upper() or None,
                "source": source,
            }
        )
    return pd.DataFrame(recs)


# ---------------------------------------------------------------------------
# Settings + training (maps 1:1 to the bespoke signals; weights are LEARNED)
# ---------------------------------------------------------------------------


# Blocking is held CONSTANT across config variants so the EM training blocks
# below stay aligned. We block on the three near-identifiers; clustering recall
# is then a function of the comparison weights, not the candidate set.
_BLOCKING = [
    block_on("phone_normalized"),
    block_on("website_identity"),
    block_on("substr(name_normalized, 1, 8)", "primary_state"),
]


def _comparisons(variant: str) -> list:
    """Comparison sets to tune. The design principle (from the multi-office
    evidence): a near-unique identifier match (website/phone) must DOMINATE, and
    weak-field DISAGREEMENT (a firm's offices differ in city/state/phone) must not
    veto it. Variants probe how to encode that."""
    name = cl.JaroWinklerAtThresholds("name_normalized", [0.92, 0.85, 0.70])
    phone_tf = cl.ExactMatch("phone_normalized").configure(term_frequency_adjustments=True)
    phone_plain = cl.ExactMatch("phone_normalized")
    # NO TF on website: a firm's own domain shared across its records is the
    # SAME-firm signal; TF down-weights big firms' domains and shatters them.
    web = cl.ExactMatch("website_identity")
    city = cl.ExactMatch("primary_city")
    state = cl.ExactMatch("primary_state")
    if variant == "base":
        return [name, phone_tf, web, city, state]
    if variant == "no_geo":
        # Drop city/state: their DISagreement was penalizing multi-office firms.
        return [name, phone_tf, web]
    if variant == "no_geo_phone_plain":
        return [name, phone_plain, web]
    if variant == "geo_no_state":
        return [name, phone_tf, web, city]
    raise ValueError(variant)


def build_settings(variant: str = "base", prob_two_random: float | None = None) -> SettingsCreator:
    kw = {}
    if prob_two_random is not None:
        # Inject the prior directly. lambda is the dominant lever: a near-unique
        # identifier's Bayes factor (~2^13 for a domain) only barely cancels a
        # tiny lambda, so website-only matches land at ~0.06 and weak-field
        # disagreement sinks them. A higher lambda (these blocked candidates are
        # match-enriched, NOT random pairs) lets a domain match be decisive —
        # the learned-model equivalent of the bespoke website floor.
        kw["probability_two_random_records_match"] = prob_two_random
    return SettingsCreator(
        link_type="dedupe_only",
        blocking_rules_to_generate_predictions=list(_BLOCKING),
        comparisons=_comparisons(variant),
        retain_intermediate_calculation_columns=True,
        **kw,
    )


def train_linker(
    df: pd.DataFrame,
    variant: str = "base",
    prob_two_random: float | str = DEFAULT_PROB_TWO_RANDOM,
) -> Linker:
    """Build + EM-train the linker (the unsupervised m/u estimation).

    ``prob_two_random`` fixes the prior lambda (default = the calibrated
    ``DEFAULT_PROB_TWO_RANDOM``); pass the string ``"estimate"`` to use Splink's
    deterministic-rule estimate instead (for sweeping/comparison)."""
    estimate = prob_two_random == "estimate"
    lam = None if estimate else float(prob_two_random)
    linker = Linker(df, build_settings(variant, lam), DuckDBAPI())
    if estimate:
        linker.training.estimate_probability_two_random_records_match(
            [block_on("phone_normalized", "website_identity")], recall=0.7
        )
    linker.training.estimate_u_using_random_sampling(max_pairs=2_000_000, seed=_U_SEED)
    for br in _BLOCKING:
        try:
            linker.training.estimate_parameters_using_expectation_maximisation(br)
        except Exception as exc:  # a degenerate block can fail EM; keep the others
            logging.getLogger(__name__).warning("EM block failed (%s): %s", br, exc)
    return linker


# ---------------------------------------------------------------------------
# Predict + cluster
# ---------------------------------------------------------------------------


def predict_pairs(linker: Linker, threshold: float = 0.01):
    """Return ({(a_id<b_id): match_probability}, prediction_frame) for every pair
    Splink scores. The frame is reused for clustering (avoids a second predict)."""
    pred = linker.inference.predict(threshold_match_probability=threshold)
    pdf = pred.as_pandas_dataframe()[["unique_id_l", "unique_id_r", "match_probability"]]
    out: dict[tuple[int, int], float] = {}
    for a, b, p in pdf.itertuples(index=False):
        a, b = int(a), int(b)
        out[(a, b) if a < b else (b, a)] = float(p)
    return out, pred


def cluster_labels(linker: Linker, pred, threshold: float) -> dict[int, str]:
    """record id -> predicted cluster id at the given probability threshold."""
    cc = linker.clustering.cluster_pairwise_predictions_at_threshold(
        pred, threshold_match_probability=threshold
    )
    cdf = cc.as_pandas_dataframe()[["unique_id", "cluster_id"]]
    return {int(r.unique_id): f"s{r.cluster_id}" for r in cdf.itertuples(index=False)}


# ---------------------------------------------------------------------------
# Fair-reference scoring helpers
# ---------------------------------------------------------------------------


def _splink_score_fn(probs: dict[tuple[int, int], float]):
    def fn(a: int, b: int) -> float:
        return probs.get((a, b) if a < b else (b, a), 0.0) * 100.0

    return fn


def _oracle_integrity(es, pred: dict[int, str]) -> list[tuple[str, int, int]]:
    """Per oracle firm: (display, n_records, n_predicted_clusters). 1 cluster = OK
    (multi-domain firms must merge into 1 — exactly where BESPOKE fails)."""
    from collections import Counter, defaultdict

    from legal_sourcing.resolution.eval_harness import ORACLE_FIRMS

    members: dict[str, list[int]] = defaultdict(list)
    for rid, fid in es.truth_by_id.items():
        members[fid].append(rid)
    out = []
    for f in ORACLE_FIRMS:
        ids_f = members.get(f"oracle:{f.key}", [])
        nclusters = len(Counter(pred.get(i) for i in ids_f)) if ids_f else 0
        out.append((f.display, len(ids_f), nclusters))
    return out


def _clerical_accuracy(es, score_fn, threshold: float) -> tuple[int, int]:
    """(correct, total) on the human-adjudicated pairs at a match threshold."""
    cler = [p for p in es.pairs if p.source == "clerical"]
    correct = sum(1 for p in cler if (score_fn(p.a_id, p.b_id) >= threshold) == bool(p.match))
    return correct, len(cler)


def _intra_firm_prob_median(es, probs: dict[tuple[int, int], float]) -> dict[str, float]:
    """Median pairwise match-prob WITHIN each oracle firm — diagnoses whether a
    split is a SCORING problem (low intra-prob) or a THRESHOLD problem."""
    from collections import defaultdict
    from statistics import median

    from legal_sourcing.resolution.eval_harness import ORACLE_FIRMS

    members: dict[str, list[int]] = defaultdict(list)
    for rid, fid in es.truth_by_id.items():
        members[fid].append(rid)
    out: dict[str, float] = {}
    for f in ORACLE_FIRMS:
        ids_f = sorted(members.get(f"oracle:{f.key}", []))
        ps = [
            probs.get((ids_f[i], ids_f[j]), 0.0)
            for i in range(len(ids_f))
            for j in range(i + 1, len(ids_f))
        ]
        out[f.display] = median(ps) if ps else float("nan")
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_TUNE_VARIANTS = ["base", "no_geo", "no_geo_phone_plain", "geo_no_state"]
_CLUSTER_THRESHOLDS = [0.5, 0.7, 0.9, 0.95, 0.99]
_PROB_SWEEP = [50, 70, 80, 90, 95, 99]


def _eval_config(es, df, label: str, variant: str, prob_two_random, helpers) -> None:
    bcubed, pairwise_sweep = helpers["bcubed"], helpers["pairwise_sweep"]
    print(f"\n========== SPLINK: {label} ==========")
    linker = train_linker(df, variant, prob_two_random)
    probs, pred = predict_pairs(linker)
    ssc = _splink_score_fn(probs)
    sbest = max(pairwise_sweep(es, ssc, _PROB_SWEEP), key=lambda p: p.f1)
    print(
        f"  pairwise best F1={sbest.f1:.3f} @ p>={sbest.threshold / 100:.2f} "
        f"(P={sbest.precision:.3f} R={sbest.recall:.3f})"
    )
    best_clu = None
    for th in _CLUSTER_THRESHOLDS:
        lab = cluster_labels(linker, pred, th)
        p, r, f1 = bcubed(es.truth_by_id, lab)
        if best_clu is None or f1 > best_clu[3]:
            best_clu = (th, p, r, f1, lab)
    th, p, r, f1, lab = best_clu
    cc, ct = _clerical_accuracy(es, ssc, sbest.threshold)
    print(f"  B-cubed best F1={f1:.3f} (P={p:.3f} R={r:.3f}) @ p>={th:.2f} | clerical {cc}/{ct}")
    meds = _intra_firm_prob_median(es, probs)
    print("  oracle integrity + intra-firm median prob:")
    for disp, n, nc in _oracle_integrity(es, lab):
        print(f"    {disp:22s} {n:3d} recs -> {nc} clusters (intra-prob med={meds[disp]:.3f})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("compare", help="Single base-config run vs bespoke.")
    pt = sub.add_parser("tune", help="Sweep comparison variants vs bespoke.")
    pt.add_argument("--variants", default=",".join(_TUNE_VARIANTS))
    pp = sub.add_parser("prior", help="Sweep the prior lambda on the base variant (the key lever).")
    pp.add_argument("--lambdas", default="estimate,1e-4,1e-3,1e-2,5e-2")
    args = ap.parse_args()

    from legal_sourcing.resolution.eval_harness import (
        bcubed,
        bespoke_clusters,
        bespoke_score_fn,
        build_eval_set,
        pairwise_sweep,
    )

    helpers = {"bcubed": bcubed, "pairwise_sweep": pairwise_sweep}
    engine = make_engine()
    with Session(engine) as session:
        print("building eval set + extracting frame (once) ...")
        es = build_eval_set(session)
        df = extract_frame(session, list(es.records))
        print(f"  {len(es.records)} records, {len(es.pairs)} labeled pairs\n")

        bsc = bespoke_score_fn(es)
        bbest = max(pairwise_sweep(es, bsc, [40, 50, 55, 60, 70, 80, 85, 88]), key=lambda p: p.f1)
        bclu = bespoke_clusters(es, merge_threshold=85.0)
        bp, br_, bf = bcubed(es.truth_by_id, bclu)
        bc_corr, bc_tot = _clerical_accuracy(es, bsc, 85.0)
        print("=== BESPOKE (reference) ===")
        print(f"  pairwise best F1={bbest.f1:.3f} (P={bbest.precision:.3f} R={bbest.recall:.3f})")
        print(f"  B-cubed F1={bf:.3f} (P={bp:.3f} R={br_:.3f}) | clerical {bc_corr}/{bc_tot}")
        print("  oracle integrity (1 cluster = OK):")
        for disp, n, nc in _oracle_integrity(es, bclu):
            print(f"    {disp:22s} {n:3d} recs -> {nc} clusters")

        if args.cmd == "prior":
            for tok in args.lambdas.split(","):
                lam = tok if tok == "estimate" else float(tok)
                _eval_config(es, df, f"lambda={tok}", "base", lam, helpers)
        else:
            variants = args.variants.split(",") if args.cmd == "tune" else ["base"]
            for variant in variants:
                _eval_config(es, df, variant, variant, DEFAULT_PROB_TWO_RANDOM, helpers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
