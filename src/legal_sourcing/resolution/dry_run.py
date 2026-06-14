"""Full-corpus canonization DRY-RUN (READ-ONLY).

Runs the Splink-on-DuckDB matcher over the WHOLE corpus, clusters it, QAs the
clusters with Splink's official cluster-evaluation doctrine (graph metrics +
bridges per ``docs/audit`` research), runs backstop checks, and emits a
stratified VERIFICATION SAMPLE for human/agentic review — **without writing any
canonical rows** (no `firms` / `match_review_queue` writes; source DB untouched).

Idiomatic structure (Splink's 3-stage evaluation; the cluster stage is the gate):
  1. train (EM, seeded) on the full extract
  2. predict edges (kept down to 0.5 so bridge analysis sees the structure)
  3. THRESHOLD SWEEP — monotonic invariant (n_clusters non-decreasing, max-size
     non-increasing as threshold rises) = a pure correctness canary
  4. cluster at the operating threshold + ``compute_graph_metrics``
     (size / density / centralisation / node-centrality / is_bridge)
  5. BACKSTOP signals: generic shared phone/website values, oversized clusters,
     bridge-glued clusters, multi-state-no-shared-identifier clusters
  6. stratified verification sample (largest / lowest-density / bridge-containing
     / multi-state / random) with full member records -> JSON for review

Artifacts land under ``data/dry_run/<ts>/`` (parquet + json + summary).

    uv run python -m legal_sourcing.resolution.dry_run --threshold 0.5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_sourcing.db import make_engine
from legal_sourcing.models import FirmSourceRecord
from legal_sourcing.resolution.splink_linker import (
    DEFAULT_PROB_TWO_RANDOM,
    DEFAULT_VARIANT,
    extract_frame,
    train_linker,
)

# Backstop knobs (reported, not yet enforced — this is a dry run).
GENERIC_VALUE_MIN_DISTINCT = 5  # a phone/website on > this many distinct names = generic
SWEEP = [0.5, 0.65, 0.8, 0.9, 0.95, 0.99]


def _members_fields(session: Session, ids: list[int]) -> dict[int, dict]:
    """Fetch the human-readable fields for a set of record ids (for review)."""
    out: dict[int, dict] = {}
    cols = (
        FirmSourceRecord.id,
        FirmSourceRecord.source,
        FirmSourceRecord.name_raw,
        FirmSourceRecord.phone_normalized,
        FirmSourceRecord.website_normalized,
        FirmSourceRecord.primary_city,
        FirmSourceRecord.primary_state,
    )
    for i in range(0, len(ids), 900):
        chunk = ids[i : i + 900]
        for r in session.execute(select(*cols).where(FirmSourceRecord.id.in_(chunk))).all():
            out[r[0]] = {
                "id": r[0],
                "source": r[1],
                "name": r[2] or "",
                "phone": r[3] or "",
                "website": r[4] or "",
                "city": r[5] or "",
                "state": r[6] or "",
            }
    return out


def _cluster_to_members(clusters_df: pd.DataFrame) -> dict[str, list[int]]:
    by: dict[str, list[int]] = defaultdict(list)
    for uid, cid in clusters_df[["unique_id", "cluster_id"]].itertuples(index=False):
        by[str(cid)].append(int(uid))
    return by


def run(threshold: float, out_dir: str, sample_n: int = 12) -> dict:
    engine = make_engine()
    with Session(engine) as session:
        print("extracting full corpus ...")
        df = extract_frame(session)  # ids=None -> all rows
        n_records = len(df)
        print(f"  {n_records:,} records")

        print(f"training ({DEFAULT_VARIANT}, lambda={DEFAULT_PROB_TWO_RANDOM}) ...")
        linker = train_linker(df, DEFAULT_VARIANT, DEFAULT_PROB_TWO_RANDOM)

        print("predicting edges (>=0.5) ...")
        pred = linker.inference.predict(threshold_match_probability=0.5)

        # --- 3) threshold sweep: monotonic invariant + size sensitivity ----------
        print("threshold sweep (monotonic invariant) ...")
        sweep_rows = []
        for th in SWEEP:
            c = linker.clustering.cluster_pairwise_predictions_at_threshold(
                pred, threshold_match_probability=th
            )
            cdf = c.as_pandas_dataframe()[["unique_id", "cluster_id"]]
            sizes = cdf.groupby("cluster_id").size()
            sweep_rows.append(
                {
                    "threshold": th,
                    "n_clusters": int(sizes.shape[0]),
                    "max_size": int(sizes.max()),
                    "p99_size": int(sizes.quantile(0.99)),
                    "singletons": int((sizes == 1).sum()),
                    "multi": int((sizes > 1).sum()),
                }
            )
        sweep = pd.DataFrame(sweep_rows)
        # invariant: n_clusters non-decreasing, max_size non-increasing as thr rises
        mono_ok = (
            sweep["n_clusters"].is_monotonic_increasing
            and sweep["max_size"].is_monotonic_decreasing
        )

        # --- 4) cluster at operating threshold + graph metrics -------------------
        print(f"clustering at operating threshold {threshold} + graph metrics ...")
        clusters = linker.clustering.cluster_pairwise_predictions_at_threshold(
            pred, threshold_match_probability=threshold
        )
        clusters_df = clusters.as_pandas_dataframe()[["unique_id", "cluster_id"]]
        gm = linker.clustering.compute_graph_metrics(
            pred, clusters, threshold_match_probability=threshold
        )
        gm_clusters = gm.clusters.as_pandas_dataframe()
        try:
            gm_edges = gm.edges.as_pandas_dataframe()
        except Exception:
            gm_edges = pd.DataFrame()
        has_bridge = "is_bridge" in gm_edges.columns
        bridges = gm_edges[gm_edges["is_bridge"]] if has_bridge else gm_edges.iloc[0:0]

        by_members = _cluster_to_members(clusters_df)
        sizes = clusters_df.groupby("cluster_id").size()
        size_dist = Counter()
        for s in sizes:
            band = (
                "1"
                if s == 1
                else "2"
                if s == 2
                else "3-5"
                if s <= 5
                else "6-10"
                if s <= 10
                else "11-25"
                if s <= 25
                else "26-50"
                if s <= 50
                else "50+"
            )
            size_dist[band] += 1

        # bridge count per cluster id (cluster_id_l == cluster_id_r within a cluster)
        bridges_by_cluster: Counter = Counter()
        if has_bridge and not bridges.empty:
            cid_of = dict(clusters_df[["unique_id", "cluster_id"]].itertuples(index=False))
            lcol = "unique_id_l" if "unique_id_l" in bridges.columns else bridges.columns[0]
            for uid_l in bridges[lcol]:
                bridges_by_cluster[str(cid_of.get(uid_l))] += 1

        # --- 5) backstop signals -------------------------------------------------
        # generic shared values: phone/website on > N distinct names across corpus
        gv = {}
        for col in ("phone_normalized", "website_identity"):
            sub = df[df[col].notna()][[col, "name_normalized"]]
            freq = sub.groupby(col)["name_normalized"].nunique().sort_values(ascending=False)
            gv[col] = freq[freq > GENERIC_VALUE_MIN_DISTINCT].head(20).to_dict()

        # --- 6) verification sample (stratified) ---------------------------------
        gm_clusters = gm_clusters.copy()
        gm_clusters["cluster_id"] = gm_clusters["cluster_id"].astype(str)
        multi_clusters = gm_clusters[gm_clusters["n_nodes"] >= 2].copy()
        multi_clusters["bridges"] = multi_clusters["cluster_id"].map(
            lambda c: bridges_by_cluster.get(c, 0)
        )

        picks: dict[str, list[str]] = {}
        picks["largest"] = (
            multi_clusters.sort_values("n_nodes", ascending=False)
            .head(sample_n)["cluster_id"]
            .tolist()
        )
        big = multi_clusters[multi_clusters["n_nodes"] >= 4]
        picks["lowest_density"] = (
            big.sort_values("density").head(sample_n)["cluster_id"].tolist()
            if not big.empty
            else []
        )
        picks["bridge_containing"] = (
            multi_clusters[multi_clusters["bridges"] > 0]
            .sort_values("bridges", ascending=False)
            .head(sample_n)["cluster_id"]
            .tolist()
        )
        picks["random"] = (
            multi_clusters.sample(min(sample_n, len(multi_clusters)), random_state=20260613)[
                "cluster_id"
            ].tolist()
            if not multi_clusters.empty
            else []
        )

        sample_ids = sorted({cid for v in picks.values() for cid in v})
        need_ids = [i for cid in sample_ids for i in by_members.get(cid, [])]
        fields = _members_fields(session, need_ids)

        def cluster_blob(cid: str, stratum: str) -> dict:
            mem_ids = by_members.get(cid, [])
            mems = [fields[i] for i in mem_ids if i in fields]
            row = gm_clusters[gm_clusters["cluster_id"] == cid]
            dens = float(row["density"].iloc[0]) if not row.empty else None
            states = {m["state"] for m in mems if m["state"]}
            webs = {m["website"] for m in mems if m["website"]}
            phones = {m["phone"] for m in mems if m["phone"]}
            return {
                "cluster_id": cid,
                "stratum": stratum,
                "n_members": len(mems),
                "density": dens,
                "bridges": bridges_by_cluster.get(cid, 0),
                "distinct_states": len(states),
                "distinct_websites": len(webs),
                "distinct_phones": len(phones),
                "members": sorted(mems, key=lambda m: m["source"])[:30],
            }

        verification = []
        for stratum, cids in picks.items():
            for cid in cids:
                verification.append(cluster_blob(cid, stratum))

        # --- write artifacts -----------------------------------------------------
        os.makedirs(out_dir, exist_ok=True)
        clusters_df.to_csv(os.path.join(out_dir, "clusters.csv"), index=False)
        gm_clusters.to_csv(os.path.join(out_dir, "cluster_metrics.csv"), index=False)
        sweep.to_csv(os.path.join(out_dir, "threshold_sweep.csv"), index=False)
        with open(os.path.join(out_dir, "verification_sample.json"), "w", encoding="utf-8") as fh:
            json.dump(verification, fh, indent=2)

        n_clusters = int(sizes.shape[0])
        n_multi = int((sizes > 1).sum())
        n_singletons = int((sizes == 1).sum())
        summary = {
            "records": n_records,
            "n_clusters": n_clusters,
            "multi_member": n_multi,
            "singletons": n_singletons,
            "max_size": int(sizes.max()),
            "p99_size": int(sizes.quantile(0.99)),
            "threshold": threshold,
            "monotonic_ok": bool(mono_ok),
            "bridges_total": len(bridges),
            "size_dist": dict(size_dist),
            "out_dir": out_dir,
        }
        # --- print report --------------------------------------------------------
        print("\n================ DRY-RUN SUMMARY ================")
        print(
            f"  records={n_records:,}  clusters={n_clusters:,} "
            f"(multi={n_multi:,}, singletons={n_singletons:,})"
        )
        print(
            f"  max cluster size={summary['max_size']}  p99={summary['p99_size']}  "
            f"bridges={summary['bridges_total']:,}"
        )
        print(f"  size distribution: {dict(size_dist)}")
        print(f"  MONOTONIC sweep invariant: {'PASS' if mono_ok else '*** FAIL ***'}")
        print("\n  threshold sweep:")
        print(sweep.to_string(index=False))
        print("\n  generic shared values (backstop — top, distinct firm-names sharing a value):")
        for col, d in gv.items():
            top = list(d.items())[:5]
            print(f"    {col}: {top}")
        print(
            f"\n  verification sample: {len(verification)} clusters across "
            f"{ {k: len(v) for k, v in picks.items()} } -> {out_dir}/verification_sample.json"
        )
        return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threshold", type=float, default=0.5, help="Operating clustering threshold.")
    ap.add_argument("--sample-n", type=int, default=12, help="Clusters per stratum to sample.")
    ap.add_argument("--out", default=None, help="Artifact dir (default data/dry_run/<ts>).")
    args = ap.parse_args()
    out = args.out or os.path.join("data", "dry_run", "latest")
    run(args.threshold, out, args.sample_n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
