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


def build_settings() -> SettingsCreator:
    return SettingsCreator(
        link_type="dedupe_only",
        blocking_rules_to_generate_predictions=[
            block_on("phone_normalized"),
            block_on("website_identity"),
            block_on("substr(name_normalized, 1, 8)", "primary_state"),
        ],
        comparisons=[
            cl.JaroWinklerAtThresholds("name_normalized", [0.92, 0.85, 0.70]),
            cl.ExactMatch("phone_normalized").configure(term_frequency_adjustments=True),
            # NO term-frequency on website: a firm's own domain shared across its
            # own records is the SAME-firm signal; TF would down-weight big firms'
            # domains and shatter them (observed: Snell&Wilmer -> 8 clusters).
            cl.ExactMatch("website_identity"),
            cl.ExactMatch("primary_city"),
            cl.ExactMatch("primary_state"),
        ],
        retain_intermediate_calculation_columns=True,
    )


def train_linker(df: pd.DataFrame) -> Linker:
    """Build + EM-train the linker (the unsupervised m/u estimation)."""
    linker = Linker(df, build_settings(), DuckDBAPI())
    # Deterministic seed for "probability two random records match".
    linker.training.estimate_probability_two_random_records_match(
        [block_on("phone_normalized", "website_identity")], recall=0.7
    )
    linker.training.estimate_u_using_random_sampling(max_pairs=2_000_000)
    for br in (
        block_on("phone_normalized"),
        block_on("website_identity"),
        block_on("substr(name_normalized, 1, 8)", "primary_state"),
    ):
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
# Head-to-head CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("compare", help="Splink vs bespoke on the identical eval set.")
    pc.add_argument(
        "--cluster-threshold",
        type=float,
        default=0.95,
        help="Probability threshold for clustering / B-cubed.",
    )
    args = ap.parse_args()

    # Imported here so eval_harness stays splink-free.
    from legal_sourcing.resolution.eval_harness import (
        ORACLE_FIRMS,
        bcubed,
        bespoke_clusters,
        bespoke_score_fn,
        build_eval_set,
        pairwise_sweep,
    )

    engine = make_engine()
    with Session(engine) as session:
        print("building eval set ...")
        es = build_eval_set(session)
        ids = list(es.records)
        print(f"extracting {len(ids)} records -> Splink frame ...")
        df = extract_frame(session, ids)

        print("training Splink (EM) ...")
        linker = train_linker(df)
        print("predicting ...")
        probs, pred = predict_pairs(linker)

        def splink_score(a: int, b: int) -> float:
            return probs.get((a, b) if a < b else (b, a), 0.0) * 100.0

        prob_thresholds = [50, 70, 80, 90, 95, 99]
        print("\n=== SPLINK pairwise sweep (match_probability * 100) ===")
        print("  thresh   TP    FP    FN     prec    rec     F1")
        best = None
        for pt in pairwise_sweep(es, splink_score, prob_thresholds):
            print(
                f"  {pt.threshold:5.0f}  {pt.tp:5d} {pt.fp:5d} {pt.fn:5d}   "
                f"{pt.precision:.3f}  {pt.recall:.3f}  {pt.f1:.3f}"
            )
            if best is None or pt.f1 > best.f1:
                best = pt
        if best:
            print(
                f"  -> SPLINK best F1={best.f1:.3f} @ p>={best.threshold / 100:.2f} "
                f"(prec={best.precision:.3f} rec={best.recall:.3f})"
            )

        # Bespoke best F1 on the same set, for the side-by-side.
        bspoke = bespoke_score_fn(es)
        bbest = max(
            pairwise_sweep(es, bspoke, [40, 50, 55, 60, 70, 80, 85, 88]),
            key=lambda p: p.f1,
        )
        print(
            f"  -> BESPOKE best F1={bbest.f1:.3f} @ {bbest.threshold:.0f} "
            f"(prec={bbest.precision:.3f} rec={bbest.recall:.3f})"
        )

        th = args.cluster_threshold
        spred = cluster_labels(linker, pred, th)
        sp, sr, sf = bcubed(es.truth_by_id, spred)
        bp, br_, bf = bcubed(es.truth_by_id, bespoke_clusters(es, merge_threshold=85.0))
        print("\n=== B-cubed ===")
        print(f"  SPLINK  @ p>={th:.2f} : precision={sp:.3f} recall={sr:.3f} F1={sf:.3f}")
        print(f"  BESPOKE @ thr=85    : precision={bp:.3f} recall={br_:.3f} F1={bf:.3f}")

        print("\n=== ORACLE firms (Splink clusters) ===")
        from collections import Counter, defaultdict

        members: dict[str, list[int]] = defaultdict(list)
        for rid, fid in es.truth_by_id.items():
            members[fid].append(rid)
        for f in ORACLE_FIRMS:
            ids_f = members.get(f"oracle:{f.key}", [])
            clusters = Counter(spred.get(i) for i in ids_f)
            verdict = (
                "OK (1 cluster)"
                if len(clusters) == 1 and ids_f
                else ("no records" if not ids_f else f"SPLIT into {len(clusters)}")
            )
            print(f"  {f.display:22s} {len(ids_f):3d} recs -> {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
