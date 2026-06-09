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
import splink.comparison_level_library as cll
import splink.comparison_library as cl
from splink import DuckDBAPI, Linker, SettingsCreator, block_on
from splink.comparison_library import CustomComparison
from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_sourcing.db import make_engine
from legal_sourcing.models import FirmSourceRecord
from legal_sourcing.resolution.identity import is_identity_website

# US Census DIVISION per state (finer than the 4 regions) — proximity proxy for
# "nearby offices are likelier the same firm" (Alex 2026-06-09: Silverman NJ/NY
# are close => likelier one firm; NY/CA far => likelier different). Same division
# (e.g. NJ+NY+PA = Middle Atlantic) earns partial state-agreement credit.
_STATE_DIVISION = {
    "CT": "NE",
    "ME": "NE",
    "MA": "NE",
    "NH": "NE",
    "RI": "NE",
    "VT": "NE",
    "NJ": "MA",
    "NY": "MA",
    "PA": "MA",
    "IL": "ENC",
    "IN": "ENC",
    "MI": "ENC",
    "OH": "ENC",
    "WI": "ENC",
    "IA": "WNC",
    "KS": "WNC",
    "MN": "WNC",
    "MO": "WNC",
    "NE": "WNC",
    "ND": "WNC",
    "SD": "WNC",
    "DE": "SA",
    "FL": "SA",
    "GA": "SA",
    "MD": "SA",
    "NC": "SA",
    "SC": "SA",
    "VA": "SA",
    "WV": "SA",
    "DC": "SA",
    "AL": "ESC",
    "KY": "ESC",
    "MS": "ESC",
    "TN": "ESC",
    "AR": "WSC",
    "LA": "WSC",
    "OK": "WSC",
    "TX": "WSC",
    "AZ": "MTN",
    "CO": "MTN",
    "ID": "MTN",
    "MT": "MTN",
    "NV": "MTN",
    "NM": "MTN",
    "UT": "MTN",
    "WY": "MTN",
    "AK": "PAC",
    "CA": "PAC",
    "HI": "PAC",
    "OR": "PAC",
    "WA": "PAC",
}

# Splink/py4j-style chatter is noisy; quiet it for CLI runs.
logging.getLogger("splink").setLevel(logging.WARNING)

# Calibrated prior: P(two random *blocked-candidate* records are the same firm).
# Splink's deterministic estimate undershoots (~1e-5, treating the whole corpus as
# the random-pair space); within blocked candidates the true match rate is ~2e-3.
# At this lambda a near-unique-identifier match becomes decisive (the learned-model
# equivalent of the bespoke website floor) WITHOUT a hand-set floor -- sweeping it
# (CLI `prior`) showed it is the dominant accuracy lever (B-cubed 0.95 -> 0.98).
DEFAULT_PROB_TWO_RANDOM = 5e-3
# Chosen production comparison set (Alex 2026-06-09, round 2): TF-name +
# name-DERIVATION containment ("zurich north america" inside "...corporate law
# division") + near-phone (Levenshtein<=1) + website + city + STATE-PROXIMITY
# (same Census division earns partial credit, so nearby offices like Silverman
# NJ/NY merge). Practice-area overlap was MEASURED and DROPPED — it split
# Morgan & Morgan and lowered precision (13% coverage / 0% on martindale).
DEFAULT_VARIANT = "tuned2_no_pa"
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
        FirmSourceRecord.practice_areas_matched,
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
    for id_, name, phone, web, city, state, source, pareas in rows:
        dom = (web or "").strip().lower() or None
        st = (state or "").strip().upper() or None
        pa = sorted({p for p in (pareas or []) if p}) or None  # None when empty -> null level
        recs.append(
            {
                "unique_id": id_,
                "name_normalized": (name or "").strip().lower() or None,
                "phone_normalized": (phone or "").strip() or None,
                "website_identity": dom if (dom and is_identity_website(dom)) else None,
                "primary_city": (city or "").strip().lower() or None,
                "primary_state": st,
                "region": _STATE_DIVISION.get(st) if st else None,
                "practice_areas": pa,
                "source": source,
            }
        )
    return pd.DataFrame(recs)


# ---------------------------------------------------------------------------
# Settings + training (maps 1:1 to the bespoke signals; weights are LEARNED)
# ---------------------------------------------------------------------------


# EM-training blocks: the near-identifiers. Kept tight so EM stays fast and each
# block has enough match signal to estimate m. (name varies within each, so its m
# is estimable.)
_EM_BLOCKING = [
    block_on("phone_normalized"),
    block_on("website_identity"),
    block_on("substr(name_normalized, 1, 8)", "primary_state"),
]
# PREDICTION blocks add a fuzzy name-prefix key WITHOUT state (Alex 2026-06-09) so
# a distinctively-named firm's offices across DIFFERENT states/sources become
# candidate pairs (West Coast Trial Lawyers NV<->CA; Silverman NJ<->NY) even with
# no shared phone/website. Term-frequency on name then keeps precision: a rare
# prefix merges, a common one ("law office") gets ~no weight despite blocking.
_PREDICTION_BLOCKING = [*_EM_BLOCKING, block_on("substr(name_normalized, 1, 10)")]


def _comparisons(variant: str) -> list:
    """Comparison sets to tune. The design principle (from the multi-office
    evidence): a near-unique identifier match (website/phone) must DOMINATE, weak-
    field DISAGREEMENT (offices differ in city/state/phone) must not veto it, and a
    DISTINCTIVE name must be able to merge on its own (rare-name term frequency)."""
    name = cl.JaroWinklerAtThresholds("name_normalized", [0.92, 0.85, 0.70])
    # TF on name: a rare name ("savela", "west coast trial lawyers") becomes a
    # strong merge signal; a common one does not -- the lever for the name-only
    # merges Alex flagged.
    name_tf = cl.JaroWinklerAtThresholds("name_normalized", [0.92, 0.85, 0.70]).configure(
        term_frequency_adjustments=True
    )
    phone_tf = cl.ExactMatch("phone_normalized").configure(term_frequency_adjustments=True)
    phone_plain = cl.ExactMatch("phone_normalized")
    # Near-phone: a Levenshtein<=1 level credits typo'd numbers (Savela ...101 vs
    # ...001) just below an exact match (Alex 2026-06-09).
    phone_near = cl.LevenshteinAtThresholds("phone_normalized", [1]).configure(
        term_frequency_adjustments=True
    )
    # NO TF on website: a firm's own domain shared across its records is the
    # SAME-firm signal; TF down-weights big firms' domains and shatters them.
    web = cl.ExactMatch("website_identity")
    city = cl.ExactMatch("primary_city")
    state = cl.ExactMatch("primary_state")
    # --- tuned2 additions (Alex 2026-06-09 round 2) ---
    # Name with a DERIVATION level: one normalized name CONTAINS the other (e.g.
    # "zurich north america" inside "zurich north america corporate law division").
    # Guarded by length>=12 so trivial common substrings ("law office") don't fire;
    # TF on the exact level keeps rare names strong. Ordered between the 0.92 and
    # 0.85 Jaro-Winkler levels.
    name_deriv = CustomComparison(
        output_column_name="name_normalized",
        comparison_description="name (exact/TF, jw, derivation-containment)",
        comparison_levels=[
            cll.NullLevel("name_normalized"),
            cll.ExactMatchLevel("name_normalized", term_frequency_adjustments=True),
            cll.JaroWinklerLevel("name_normalized", 0.92),
            cll.CustomLevel(
                sql_condition=(
                    "(LENGTH(name_normalized_l) >= 12 AND "
                    "name_normalized_r LIKE '%' || name_normalized_l || '%') OR "
                    "(LENGTH(name_normalized_r) >= 12 AND "
                    "name_normalized_l LIKE '%' || name_normalized_r || '%')"
                ),
                label_for_charts="one name contains the other (derivation)",
            ),
            cll.JaroWinklerLevel("name_normalized", 0.85),
            cll.JaroWinklerLevel("name_normalized", 0.70),
            cll.ElseLevel(),
        ],
    )
    # State with PROXIMITY: exact state > same Census division (nearby) > else.
    state_prox = CustomComparison(
        output_column_name="primary_state",
        comparison_description="state with regional proximity",
        comparison_levels=[
            cll.NullLevel("primary_state"),
            cll.ExactMatchLevel("primary_state"),
            cll.CustomLevel(
                sql_condition="region_l = region_r AND region_l IS NOT NULL",
                label_for_charts="same census division (nearby)",
            ),
            cll.ElseLevel(),
        ],
    )
    # Practice-area overlap (sparse: 13% coverage, 0% martindale) — weak corroborator.
    pareas = CustomComparison(
        output_column_name="practice_areas",
        comparison_description="practice-area overlap",
        comparison_levels=[
            cll.NullLevel("practice_areas"),
            cll.ArrayIntersectLevel("practice_areas", min_intersection=2),
            cll.ArrayIntersectLevel("practice_areas", min_intersection=1),
            cll.ElseLevel(),
        ],
    )
    if variant == "base":
        return [name, phone_tf, web, city, state]
    if variant == "tuned":  # the Alex-2026-06-09 design
        return [name_tf, phone_near, web, city, state]
    if variant == "tuned2":  # + name-derivation, state proximity, practice areas
        return [name_deriv, phone_near, web, city, state_prox, pareas]
    if variant == "tuned2_no_pa":  # isolate whether practice areas help
        return [name_deriv, phone_near, web, city, state_prox]
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
        blocking_rules_to_generate_predictions=list(_PREDICTION_BLOCKING),
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
    for br in _EM_BLOCKING:
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


def export_active_sample(es, probs, out: str, n: int = 40, seed: int = _U_SEED) -> int:
    """Active-learning export for human review: the MOST informative unlabeled
    pairs — Splink's UNCERTAIN band (0.4-0.7, the decision boundary) + its
    HIGH-confidence merges the website-labeler disputes (precision spot-checks).
    Writes a CSV with record context + an empty clerical_match column."""
    import csv
    import os
    import random

    rng = random.Random(seed)
    rec = es.records
    labeled = {(p.a_id, p.b_id) for p in es.pairs}
    auto = {(p.a_id, p.b_id): p.match for p in es.pairs}
    uncertain, high_disagree = [], []
    for (a, b), pv in probs.items():
        key = (a, b) if a < b else (b, a)
        if key in labeled:
            continue
        if 0.40 <= pv <= 0.70:
            uncertain.append((*key, pv, "uncertain"))
        elif pv >= 0.97 and auto.get(key) == 0:
            high_disagree.append((*key, pv, "high_conf_vs_label"))
    rng.shuffle(uncertain)
    rng.shuffle(high_disagree)
    picks = uncertain[: n - n // 2] + high_disagree[: n // 2]
    rows = []
    for a, b, pv, cat in picks:
        ra, rb = rec[a], rec[b]
        rows.append(
            {
                "a_id": a,
                "b_id": b,
                "splink_prob": round(pv, 3),
                "category": cat,
                "a_name": ra.name_raw or "",
                "b_name": rb.name_raw or "",
                "a_src": ra.source,
                "b_src": rb.source,
                "a_web": ra.website_normalized or "",
                "b_web": rb.website_normalized or "",
                "a_phone": ra.phone_normalized or "",
                "b_phone": rb.phone_normalized or "",
                "a_city": f"{ra.primary_city or ''},{ra.primary_state or ''}",
                "b_city": f"{rb.primary_city or ''},{rb.primary_state or ''}",
                "clerical_match": "",
            }
        )
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if rows:
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    return len(rows)


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

_TUNE_VARIANTS = ["base", "tuned", "tuned2_no_pa", "tuned2"]
_CLUSTER_THRESHOLDS = [0.5, 0.7, 0.9, 0.95, 0.99]
_PROB_SWEEP = [50, 70, 80, 90, 95, 99]


def derive_operating_threshold(linker: Linker, es) -> tuple[float, float | None]:
    """IDIOMATIC threshold selection (no hand-set number): register the labeled
    pairs and let Splink's ``accuracy_analysis_from_labels_table`` pick the
    F1-optimal match-probability. That is the data-derived operating point — it
    adapts to the corpus (e.g. lands below the ~0.89 floor that domain-only merges
    like eapdlaw need, instead of an arbitrary 0.9)."""
    labels = pd.DataFrame(
        {
            "unique_id_l": [min(p.a_id, p.b_id) for p in es.pairs],
            "unique_id_r": [max(p.a_id, p.b_id) for p in es.pairs],
            "clerical_match_score": [float(p.match) for p in es.pairs],
        }
    )
    try:
        lab = linker.table_management.register_labels_table(labels, "eval_labels")
        tbl = linker.evaluation.accuracy_analysis_from_labels_table(
            lab, output_type="table", add_metrics=["f1"]
        ).as_pandas_dataframe()
    except Exception as exc:  # be resilient; fall back to a sweep if the API shifts
        logging.getLogger(__name__).warning("threshold derivation failed: %s", exc)
        return 0.85, None
    f1col = next((c for c in tbl.columns if c.lower() == "f1"), None)
    pcol = next((c for c in tbl.columns if c.lower() == "truth_threshold_probability"), None)
    pcol = pcol or next((c for c in tbl.columns if "probability" in c.lower()), None)
    if f1col is None or pcol is None:
        logging.getLogger(__name__).warning("accuracy table cols: %s", list(tbl.columns))
        return 0.85, None
    row = tbl.loc[tbl[f1col].idxmax()]
    return float(row[pcol]), float(row[f1col])


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
    # Idiomatic, data-derived operating threshold (Splink chooses it).
    thr, thr_f1 = derive_operating_threshold(linker, es)
    lab = cluster_labels(linker, pred, thr)
    p, r, f1 = bcubed(es.truth_by_id, lab)
    cc, ct = _clerical_accuracy(es, ssc, thr * 100)
    print(
        f"  Splink-DERIVED operating threshold = {thr:.3f} (labeled-set F1={thr_f1})\n"
        f"  B-cubed @ derived: F1={f1:.3f} (P={p:.3f} R={r:.3f}) | clerical {cc}/{ct}"
    )
    meds = _intra_firm_prob_median(es, probs)
    print("  oracle integrity + intra-firm median prob:")
    for disp, n, nc in _oracle_integrity(es, lab):
        print(f"    {disp:22s} {n:3d} recs -> {nc} clusters (intra-prob med={meds[disp]:.3f})")


def _inspect_errors(es, probs, cap: int = 20) -> None:
    """Print the pairs where Splink (default lambda) disagrees with the eval
    LABELS — to judge whether 'false positives' are real errors or the biased
    auto-labeler penalizing correct multi-domain merges."""
    rec = es.records

    def fmt(rid: int) -> str:
        r = rec[rid]
        return (
            f"[{r.source[:4]}] {r.name_raw or '(no name)'!r:40.40} "
            f"web={r.website_normalized or '-':24.24} ph={r.phone_normalized or '-':13} "
            f"{r.primary_city or '-'},{r.primary_state or '-'}"
        )

    def p(a, b):
        return probs.get((a, b) if a < b else (b, a), 0.0)

    # Every human-labeled (clerical) pair with its prob + verdict — the truest check.
    cler = [pr for pr in es.pairs if pr.source == "clerical"]
    if cler:
        print(f"\n=== CLERICAL pairs ({len(cler)}) — prob @ p>=0.5 verdict ===")
        for pr in sorted(cler, key=lambda x: (x.match, -p(x.a_id, x.b_id))):
            pv = p(pr.a_id, pr.b_id)
            ok = "OK " if (pv >= 0.5) == bool(pr.match) else "XX "
            tag = "same" if pr.match else "diff"
            print(f"  {ok} label={tag} p={pv:.3f}  {pr.a_id}/{pr.b_id}")
            if ok == "XX ":
                print(f"        A {fmt(pr.a_id)}\n        B {fmt(pr.b_id)}")

    fps = sorted(
        ((pr.a_id, pr.b_id, p(pr.a_id, pr.b_id)) for pr in es.pairs if pr.match == 0),
        key=lambda t: -t[2],
    )
    fps = [t for t in fps if t[2] >= 0.5]
    print(f"\n=== FALSE POSITIVES (label=different, Splink>=0.5): {len(fps)} ===")
    for a, b, pr in fps[:cap]:
        print(f"  p={pr:.3f}\n    A {fmt(a)}\n    B {fmt(b)}")
    fns = sorted(
        ((pr.a_id, pr.b_id, p(pr.a_id, pr.b_id)) for pr in es.pairs if pr.match == 1),
        key=lambda t: t[2],
    )
    fns = [t for t in fns if t[2] < 0.5]
    print(f"\n=== FALSE NEGATIVES (label=same, Splink<0.5): {len(fns)} ===")
    for a, b, pr in fns[:cap]:
        print(f"  p={pr:.3f}\n    A {fmt(a)}\n    B {fmt(b)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("compare", help="Single base-config run vs bespoke.")
    pt = sub.add_parser("tune", help="Sweep comparison variants vs bespoke.")
    pt.add_argument("--variants", default=",".join(_TUNE_VARIANTS))
    pp = sub.add_parser("prior", help="Sweep the prior lambda on a variant (the key lever).")
    pp.add_argument("--lambdas", default="estimate,1e-4,1e-3,1e-2,5e-2")
    pp.add_argument("--variant", default="tuned")
    sub.add_parser("errors", help="Inspect Splink's FP/FN pairs vs the eval labels.")
    psa = sub.add_parser("sample", help="Export active-learning pairs (uncertain + disagreements).")
    psa.add_argument("--n", type=int, default=40)
    psa.add_argument("--out", default="data/eval/active_sample.csv")
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
                _eval_config(es, df, f"{args.variant} lambda={tok}", args.variant, lam, helpers)
        elif args.cmd == "errors":
            linker = train_linker(df, DEFAULT_VARIANT, DEFAULT_PROB_TWO_RANDOM)
            probs, _ = predict_pairs(linker)
            _inspect_errors(es, probs)
        elif args.cmd == "sample":
            linker = train_linker(df, DEFAULT_VARIANT, DEFAULT_PROB_TWO_RANDOM)
            probs, _ = predict_pairs(linker)
            k = export_active_sample(es, probs, args.out, args.n)
            print(f"wrote {k} active-learning pairs -> {args.out}")
        else:
            variants = args.variants.split(",") if args.cmd == "tune" else [DEFAULT_VARIANT]
            for variant in variants:
                _eval_config(es, df, variant, variant, DEFAULT_PROB_TWO_RANDOM, helpers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
