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
from legal_sourcing.normalize.firm_name import firm_name_core
from legal_sourcing.resolution.apply import _UnionFind
from legal_sourcing.resolution.eval_harness import CLERICAL_LABELS_PATH, _load_clerical_labels
from legal_sourcing.resolution.must_link import build_redirect_map, website_must_link_edges
from legal_sourcing.resolution.splink_linker import (
    DEFAULT_PROB_TWO_RANDOM,
    DEFAULT_VARIANT,
    extract_frame,
    train_linker,
)

# Backstop knobs.
GENERIC_VALUE_MIN_DISTINCT = 5  # a phone/website on > this many distinct names = generic
# A name token is DISTINCTIVE if it appears in <= this many distinct firm cores.
# Brand tokens (zurich, claimshero, weintraub) are rare; common first/surnames
# (michael, smith, christopher, law, group) are not. Two records sharing a
# distinctive token may merge on name alone; sharing only common tokens may not.
RARE_TOKEN_MAX_FIRMS = 40
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


def _generic_value_sets(df: pd.DataFrame, k: int) -> tuple[set, set]:
    """Phones / identity-websites that appear on > k DISTINCT firm name-cores —
    i.e. shared across many firms (lead-gen lines, .gov domains, marketing
    platforms). Blank-name records (NaN core) don't count as distinct firms, so a
    firm's own domain on 50 blank Justia rows + 1 named row stays NON-generic."""
    gp, gw = set(), set()
    for col, out in (("phone_normalized", gp), ("website_identity", gw)):
        sub = df[df[col].notna()][[col, "name_core_key"]]
        freq = sub.groupby(col)["name_core_key"].nunique()  # nunique ignores NaN
        out.update(freq[freq > k].index.tolist())
    return gp, gw


def _uf_components(edges: list[tuple[int, int]], all_ids: list[int]) -> dict[int, list[int]]:
    uf = _UnionFind()
    for i in all_ids:
        uf.find(i)
    for a, b in edges:
        uf.union(a, b)
    return uf.components(all_ids)


def backstop_kept_edges(
    df: pd.DataFrame, all_edges: list[tuple[int, int]]
) -> tuple[list, set, set]:
    """THE BACKSTOP. Keep an edge iff the pair shares a NON-GENERIC strong
    identifier (phone / identity-website) OR a DISTINCTIVE (rare) name token.
    Returns (kept_edges, generic_phones, generic_websites). Shared by the dry-run
    and the canonical write so both use identical filtering."""
    gp, gw = _generic_value_sets(df, GENERIC_VALUE_MIN_DISTINCT)
    rec_ph = dict(zip(df["unique_id"], df["phone_normalized"], strict=False))
    rec_web = dict(zip(df["unique_id"], df["website_identity"], strict=False))
    rec_tokens = {
        int(i): tuple(firm_name_core(n)) if isinstance(n, str) and n else ()
        for i, n in zip(df["unique_id"], df["name_normalized"], strict=False)
    }
    # token document-frequency = # of DISTINCT firm cores a token appears in (dedup
    # by full token tuple so "michael j whelan"/"michael j ekdahl" count separately
    # -> "michael"/"j" come out common, brand tokens like "zurich" rare).
    tok_df: Counter = Counter()
    seen_cores: set = set()
    for toks in rec_tokens.values():
        if not toks or toks in seen_cores:
            continue
        seen_cores.add(toks)
        for t in set(toks):
            tok_df[t] += 1
    rare = {t for t, n in tok_df.items() if n <= RARE_TOKEN_MAX_FIRMS}
    kept = []
    for a, b in all_edges:
        pa, pb = rec_ph.get(a), rec_ph.get(b)
        wa, wb = rec_web.get(a), rec_web.get(b)
        strong = (pa and pa == pb and pa not in gp) or (wa and wa == wb and wa not in gw)
        shared_rare = bool((set(rec_tokens.get(a, ())) & set(rec_tokens.get(b, ()))) & rare)
        if strong or shared_rare:
            kept.append((a, b))
    return kept, gp, gw


def compute_backstop_clusters(
    session, threshold: float = 0.5, *, must_link: bool = False
) -> dict[int, list[int]]:
    """The production clustering: extract -> train -> predict -> BACKSTOP edge
    filter (+ optional website MUST-LINK recall pass) -> connected components.
    Returns {root_id: [member record ids]} over ALL records (singletons included).
    Used by the canonical write in apply.py. ``must_link`` is opt-in (default off)
    so the canonical write is unchanged until the recall pass is signed off."""
    df = extract_frame(session)
    linker = train_linker(df, DEFAULT_VARIANT, DEFAULT_PROB_TWO_RANDOM)
    pred = linker.inference.predict(threshold_match_probability=threshold)
    edges_pdf = pred.as_pandas_dataframe()[["unique_id_l", "unique_id_r", "match_probability"]]
    edges_pdf = edges_pdf[edges_pdf["match_probability"] >= threshold]
    all_edges = [(int(a), int(b)) for a, b, _ in edges_pdf.itertuples(index=False)]
    kept, _, _ = backstop_kept_edges(df, all_edges)
    if must_link:
        ml_edges, _, _ = website_must_link_edges(df, build_redirect_map(session))
        kept = kept + ml_edges
    return _uf_components(kept, [int(x) for x in df["unique_id"]])


def _size_bands(sizes) -> Counter:
    out: Counter = Counter()
    for s in sizes:
        out[
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
        ] += 1
    return out


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

        # --- 4b) BACKSTOP: pre-cluster edge filter -------------------------------
        # Keep an edge iff the pair shares a NON-GENERIC strong identifier (phone /
        # identity-website) OR a DISTINCTIVE (rare) name token. Verified: over-merges
        # rest on a weak/generic link (a common name like "michael"/"smith", or a
        # phone/domain shared across many firms); legitimate low-corroboration merges
        # share a rare brand token (zurich, claimshero) or their own phone/website.
        print("backstop: edge filter (shared non-generic strong id OR rare name token) ...")
        edges_pdf = pred.as_pandas_dataframe()[["unique_id_l", "unique_id_r", "match_probability"]]
        edges_pdf = edges_pdf[edges_pdf["match_probability"] >= threshold]
        all_edges = [(int(a), int(b)) for a, b, _ in edges_pdf.itertuples(index=False)]
        kept, gp, gw = backstop_kept_edges(df, all_edges)
        all_ids = [int(x) for x in df["unique_id"]]
        comps_base = _uf_components(all_edges, all_ids)
        comps_bs = _uf_components(kept, all_ids)
        root_base = {i: r for r, mem in comps_base.items() for i in mem}
        root_bs = {i: r for r, mem in comps_bs.items() for i in mem}

        # --- MUST-LINK recall pass (FN under-merges): ADD edges where records share
        # their firm's OWN identity domain (redirect-aware) but split because names
        # differ. Generic-domain gated (Gate A) + mis-attribution guarded (Gate B). -
        print("must-link: website recall pass (shared identity domain, redirect-aware) ...")
        redirect_map = build_redirect_map(session)
        ml_edges, ml_stats, ml_groups = website_must_link_edges(df, redirect_map)
        comps_ml = _uf_components(kept + ml_edges, all_ids)
        root_ml = {i: r for r, mem in comps_ml.items() for i in mem}
        ml_sizes = [len(v) for v in comps_ml.values()]

        # --- LABEL REGRESSION: the known-answer merge/not-merge list -------------
        # The "backstop" in the test-set sense: every labeled pair must come out
        # right. Compare Splink-alone (root_base) vs +edge-filter (root_bs) vs
        # +must-link (root_ml).
        labels = _load_clerical_labels(CLERICAL_LABELS_PATH)
        reg = {
            "total": 0,
            "base_ok": 0,
            "bs_ok": 0,
            "ml_ok": 0,
            "bs_fail": [],
            "base_fail": [],
            "ml_fail": [],
        }
        present_ids = set(all_ids)
        for a, b, lab in labels:
            if a not in present_ids or b not in present_ids:
                continue
            reg["total"] += 1
            base_merged = root_base.get(a) == root_base.get(b)
            bs_merged = root_bs.get(a) == root_bs.get(b)
            if base_merged == bool(lab):
                reg["base_ok"] += 1
            else:
                reg["base_fail"].append((a, b, lab, base_merged))
            if bs_merged == bool(lab):
                reg["bs_ok"] += 1
            else:
                reg["bs_fail"].append((a, b, lab, bs_merged))
            ml_merged = root_ml.get(a) == root_ml.get(b)
            if ml_merged == bool(lab):
                reg["ml_ok"] += 1
            else:
                reg["ml_fail"].append((a, b, lab, ml_merged))
        base_sizes = [len(v) for v in comps_base.values()]
        bs_sizes = [len(v) for v in comps_bs.values()]

        def _n_components(ids, root):
            return len({root.get(i) for i in ids})

        # preservation: known-good single-firm website groups must stay 1 component
        rec_web = dict(zip(df["unique_id"], df["website_identity"], strict=False))
        preserve = {}
        for dom in ("forthepeople.com", "kutakrock.com", "hollandhart.com", "swlaw.com"):
            ids = [int(i) for i, w in rec_web.items() if w == dom]
            if ids:
                preserve[dom] = (
                    len(ids),
                    _n_components(ids, root_base),
                    _n_components(ids, root_bs),
                )
        # shatter: common-name hairball records must split into many components
        shatter = {}
        for tok in ("christopher", "michael j", "smith law"):
            ids = [
                int(r.unique_id)
                for r in df[df["name_normalized"].fillna("").str.startswith(tok)].itertuples()
            ]
            if ids:
                shatter[tok] = (
                    len(ids),
                    _n_components(ids, root_base),
                    _n_components(ids, root_bs),
                )

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
        print("\n  generic shared values (top, distinct firm-names sharing a value):")
        for col, d in gv.items():
            top = list(d.items())[:5]
            print(f"    {col}: {top}")

        # --- BACKSTOP before/after -------------------------------------------
        def _tailcount(sz, n):
            return sum(1 for s in sz if s > n)

        print("\n================ BACKSTOP: edge filter (non-generic strong id) ================")
        print(f"  generic values suppressed: {len(gp)} phones, {len(gw)} websites")
        print(
            f"  edges >= {threshold}: {len(all_edges):,} -> kept {len(kept):,} "
            f"({len(all_edges) - len(kept):,} dropped, name/city/generic-only)"
        )
        print(f"  max cluster size:   {max(base_sizes):>6,}  ->  {max(bs_sizes):>6,}")
        print(
            f"  clusters > 25:      {_tailcount(base_sizes, 25):>6,}  ->  {_tailcount(bs_sizes, 25):>6,}"
        )
        print(
            f"  clusters > 50:      {_tailcount(base_sizes, 50):>6,}  ->  {_tailcount(bs_sizes, 50):>6,}"
        )
        print(
            f"  multi-member:       {sum(1 for s in base_sizes if s > 1):>6,}  ->  "
            f"{sum(1 for s in bs_sizes if s > 1):>6,}"
        )
        print("  PRESERVE (known one-firm website groups; n_recs: base_comps -> bs_comps, want 1):")
        for dom, (n, b, a) in preserve.items():
            flag = "OK" if a == 1 else "** SPLIT **"
            print(f"    {dom:20s} {n:4d} recs: {b} -> {a}  {flag}")
        print("  SHATTER (common-name hairballs; n_recs: base_comps -> bs_comps, want many):")
        for tok, (n, b, a) in shatter.items():
            print(f"    {tok:16s} {n:4d} recs: {b} -> {a}")
        print(
            f"\n  LABEL REGRESSION ({reg['total']} known-answer pairs): "
            f"Splink-alone {reg['base_ok']}/{reg['total']}  ->  Splink+backstop {reg['bs_ok']}/{reg['total']}"
        )
        if reg["bs_fail"]:
            print("    backstop still-wrong pairs (a/b label got):")
            for a, b, lab, merged in reg["bs_fail"]:
                print(
                    f"      {a}/{b} want={'merge' if lab else 'split'} got={'merge' if merged else 'split'}"
                )
        summary["label_regression"] = {
            "total": reg["total"],
            "base_ok": reg["base_ok"],
            "bs_ok": reg["bs_ok"],
        }
        summary["backstop"] = {
            "generic_phones": len(gp),
            "generic_websites": len(gw),
            "edges": len(all_edges),
            "edges_kept": len(kept),
            "max_size_base": max(base_sizes),
            "max_size_bs": max(bs_sizes),
            "clusters_gt50_base": _tailcount(base_sizes, 50),
            "clusters_gt50_bs": _tailcount(bs_sizes, 50),
        }

        # --- MUST-LINK: recovery + guards ----------------------------------------
        recovered = sum(
            1
            for g in ml_groups.values()
            if len({root_bs.get(i) for i in g["members"]}) > 1
            and len({root_ml.get(i) for i in g["members"]}) == 1
        )
        still_split = sum(
            1 for g in ml_groups.values() if len({root_ml.get(i) for i in g["members"]}) > 1
        )
        print("\n================ MUST-LINK: website recall pass (FN recovery) ================")
        print(
            f"  cross-domain redirects known: {len(redirect_map):,} "
            f"(e.g. shermanhoward.com -> {redirect_map.get('shermanhoward.com', '?')})"
        )
        print(
            f"  domains linked: {ml_stats.get('domains_linked', 0):,}  "
            f"edges added: {ml_stats.get('edges', 0):,}  "
            f"records pulled in: {ml_stats.get('records_linked', 0):,}"
        )
        print(
            f"  Gate A generic domains skipped: {ml_stats.get('generic_domains_skipped', 0):,}  "
            f"Gate B mis-attribution excluded: {ml_stats.get('misattribution_excluded', 0):,}"
        )
        print(
            f"  FN under-merges RECOVERED (domain split -> 1 firm): {recovered:,}  "
            f"(still split: {still_split:,})"
        )
        print(
            f"  cluster size impact: max {max(bs_sizes):,} -> {max(ml_sizes):,}  "
            f"clusters>50 {_tailcount(bs_sizes, 50):,} -> {_tailcount(ml_sizes, 50):,}"
        )
        print(
            f"  LABEL REGRESSION +must-link: {reg['ml_ok']}/{reg['total']} "
            f"(backstop-alone {reg['bs_ok']}/{reg['total']})"
        )
        if reg["ml_fail"]:
            print("    must-link regressions/misses (a/b want got):")
            for a, b, lab, merged in reg["ml_fail"]:
                print(
                    f"      {a}/{b} want={'merge' if lab else 'split'} "
                    f"got={'merge' if merged else 'split'}"
                )
        print("  TARGETED DOMAIN CHECKS (members: bs_comps -> ml_comps; FNs want ->1):")
        for dom in ("hbsslaw.com", "knchlaw.com", "bbglaw.com", "gdflaw.com", "zellaw.com", "rlb.com"):
            g = ml_groups.get(dom)
            if not g:
                print(f"    {dom:24s}  (not present / no non-generic group)")
                continue
            mem = g["members"]
            bs_c = len({root_bs.get(i) for i in mem})
            ml_c = len({root_ml.get(i) for i in mem})
            print(
                f"    {dom:24s}  {len(mem):3d} recs: {bs_c} -> {ml_c} comps; "
                f"excluded(mis-attr)={len(g['excluded'])}"
            )

        # verification sample: newly-merged groups + any with mis-attribution exclusions
        def _dom_blob(dom: str) -> dict:
            g = ml_groups[dom]
            f = _members_fields(session, g["members"])
            return {
                "domain": dom,
                "redirect_to": redirect_map.get(dom),
                "bs_components": len({root_bs.get(i) for i in g["members"]}),
                "ml_components": len({root_ml.get(i) for i in g["members"]}),
                "excluded_misattribution": [f.get(i, {"id": i}) for i in g["excluded"]],
                "members": [f[i] for i in g["members"] if i in f][:30],
            }

        recovered_doms = [
            d
            for d, g in ml_groups.items()
            if len({root_bs.get(i) for i in g["members"]}) > 1
            and len({root_ml.get(i) for i in g["members"]}) == 1
        ][:25]
        excluded_doms = [d for d, g in ml_groups.items() if g["excluded"]][:15]
        mustlink_sample = {
            "recovered": [_dom_blob(d) for d in recovered_doms],
            "with_exclusions": [_dom_blob(d) for d in excluded_doms],
        }
        with open(os.path.join(out_dir, "mustlink_sample.json"), "w", encoding="utf-8") as fh:
            json.dump(mustlink_sample, fh, indent=2)

        summary["must_link"] = {
            "redirects_known": len(redirect_map),
            "domains_linked": ml_stats.get("domains_linked", 0),
            "edges_added": ml_stats.get("edges", 0),
            "records_linked": ml_stats.get("records_linked", 0),
            "generic_skipped": ml_stats.get("generic_domains_skipped", 0),
            "misattribution_excluded": ml_stats.get("misattribution_excluded", 0),
            "fn_recovered": recovered,
            "max_size_ml": max(ml_sizes),
            "label_ml_ok": reg["ml_ok"],
        }

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
