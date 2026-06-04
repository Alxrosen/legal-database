"""Sample-based evaluation harness for canonical firm resolution.

Pulls a small neighborhood of source records around a seed (website / phone /
name / random), runs the REAL blocking -> scoring -> union-find clustering on
just that subset, fuses each cluster with ``resolution.fusion.fuse_cluster``,
and prints the canonical firm(s) + members + provenance for human inspection.

It does NOT write to the database -- pure read + in-memory compute -- so it is
safe to run against the live shared DB while scrapes are in progress, and lets
us iterate on the fusion/scoring without touching the canonical tables.

Examples::

    uv run python -m legal_sourcing.resolution.sample_eval --website swlaw.com
    uv run python -m legal_sourcing.resolution.sample_eval --phone +18336461198
    uv run python -m legal_sourcing.resolution.sample_eval --name snell
    uv run python -m legal_sourcing.resolution.sample_eval --random 5
    uv run python -m legal_sourcing.resolution.sample_eval --website swlaw.com --merge-threshold 60
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from legal_sourcing.db import make_engine
from legal_sourcing.models import FirmSourceRecord, WebsiteEnrichment
from legal_sourcing.normalize.url import is_aggregator_domain
from legal_sourcing.resolution.apply import _UnionFind
from legal_sourcing.resolution.blocking import generate_candidate_pairs
from legal_sourcing.resolution.fusion import fuse_cluster
from legal_sourcing.resolution.scoring import score_pair


def _derive_location(rec: FirmSourceRecord) -> None:
    """Populate primary_city/primary_state from offices JSON if empty -- mirrors
    the Mastermind backfill, so --derive-location previews post-backfill
    clustering (the name+city+state floor) without writing the DB."""
    if (rec.primary_state or "").strip():
        return
    chosen = None
    for o in rec.offices or []:
        nm = o.get("normalized") or {}
        city = nm.get("city") or o.get("city_raw")
        state = nm.get("state") or o.get("state_raw")
        if not (city or state):
            continue
        if o.get("is_primary"):
            chosen = (city, state)
            break
        if chosen is None:
            chosen = (city, state)
    if chosen:
        rec.primary_city, rec.primary_state = chosen


def _expand(session: Session, seed: list[FirmSourceRecord], *, max_records: int, max_hops: int = 3):
    """BFS over phone/website blocking keys from the seed to gather the
    (approximate) full connected neighborhood. Capped for speed."""
    seen: dict[int, FirmSourceRecord] = {r.id: r for r in seed}
    frontier = list(seed)
    for _ in range(max_hops):
        phones = {r.phone_normalized for r in frontier if r.phone_normalized}
        webs = {
            r.website_normalized
            for r in frontier
            if r.website_normalized and not is_aggregator_domain(r.website_normalized)
        }
        conds = []
        if phones:
            conds.append(FirmSourceRecord.phone_normalized.in_(phones))
        if webs:
            conds.append(FirmSourceRecord.website_normalized.in_(webs))
        if not conds:
            break
        rows = session.scalars(select(FirmSourceRecord).where(or_(*conds))).all()
        new = [r for r in rows if r.id not in seen]
        for r in new:
            seen[r.id] = r
        if not new or len(seen) >= max_records:
            break
        frontier = new
    return list(seen.values())


def _cluster(records: list[FirmSourceRecord], merge_threshold: float):
    """Run blocking + scoring + union-find at the given merge threshold."""
    by_id = {r.id: r for r in records}
    uf = _UnionFind()
    for r in records:
        uf.find(r.id)
    edges = 0
    for a, b in generate_candidate_pairs(records):
        if score_pair(by_id[a], by_id[b])["total"] >= merge_threshold:
            uf.union(a, b)
            edges += 1
    return uf.components(list(by_id)), edges


def _enrichment_for(session: Session, members: list[FirmSourceRecord]) -> WebsiteEnrichment | None:
    webs = [
        m.website_normalized
        for m in members
        if m.website_normalized and not is_aggregator_domain(m.website_normalized)
    ]
    if not webs:
        return None
    web = Counter(webs).most_common(1)[0][0]
    return session.scalars(
        select(WebsiteEnrichment).where(WebsiteEnrichment.website == web)
    ).first()


def _print_cluster(session: Session, members: list[FirmSourceRecord]) -> None:
    enr = _enrichment_for(session, members)
    res = fuse_cluster(members, enr)
    print(f"\n  --- canonical firm ({len(members)} members) ---")
    print(f"    name      : {res.name!r}  (norm={res.name_normalized!r})")
    print(f"    website   : {res.website_normalized}   phone: {res.phone_normalized}")
    print(f"    attorneys : {res.attorney_count}   year_founded: {res.year_founded}")
    print(f"    sources   : {dict(Counter(m.source for m in members))}")
    if enr is not None:
        print(
            f"    enrichment: acount_min={enr.attorney_count_min} verif={enr.url_verification_status} "
            f"offices={enr.office_count} scope={enr.scope}"
        )
    if res.notes:
        print(f"    notes     : {res.notes}")
    for f, p in res.field_provenance.items():
        flags = "".join(
            f" {k}={p[k]}" for k in ("is_min", "variants", "groups", "approximate") if k in p
        )
        print(
            f"      prov {f:14s}: {p.get('method')} src={p.get('source')} "
            f"support={p.get('support')} conf={p.get('confidence')}{flags}"
        )
    names = Counter((m.source, (m.name_raw or "")) for m in members)
    print("    member (source, name) [top 8]:")
    for (src, nm), n in names.most_common(8):
        print(f"      {n:3d} {src:10s} {nm!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--website")
    g.add_argument("--phone")
    g.add_argument("--name")
    g.add_argument("--random", type=int)
    ap.add_argument("--merge-threshold", type=float, default=85.0)
    ap.add_argument("--max-records", type=int, default=2500)
    ap.add_argument(
        "--derive-location",
        action="store_true",
        help="Derive primary_city/state from offices JSON in-memory to preview "
        "post-backfill clustering (the name+city+state floor).",
    )
    ap.add_argument("--show", type=int, default=8, help="How many largest components to print.")
    args = ap.parse_args()

    engine = make_engine()
    with Session(engine) as session:
        if args.website:
            seed = session.scalars(
                select(FirmSourceRecord).where(FirmSourceRecord.website_normalized == args.website)
            ).all()
        elif args.phone:
            seed = session.scalars(
                select(FirmSourceRecord).where(FirmSourceRecord.phone_normalized == args.phone)
            ).all()
        elif args.name:
            seed = session.scalars(
                select(FirmSourceRecord)
                .where(FirmSourceRecord.name_normalized.like(f"%{args.name.lower()}%"))
                .limit(60)
            ).all()
        else:
            seed = session.scalars(
                select(FirmSourceRecord).order_by(func.random()).limit(args.random)
            ).all()

        seed_ids = {r.id for r in seed}
        print(f"seed records: {len(seed)}")
        records = _expand(session, seed, max_records=args.max_records)
        if args.derive_location:
            for r in records:
                _derive_location(r)
        comps, edges = _cluster(records, args.merge_threshold)
        multi = {root: ids for root, ids in comps.items() if len(ids) > 1}
        print(
            f"neighborhood: {len(records)} records | merge_threshold={args.merge_threshold} | "
            f"merge_edges={edges}"
        )
        print(
            f"components: {len(comps)} ({len(multi)} multi-member, "
            f"{len(comps) - len(multi)} singletons)"
        )
        # How did the seed records scatter across components? (fragmentation)
        seed_comp_sizes = Counter()
        for root, ids in comps.items():
            n_seed = sum(1 for i in ids if i in seed_ids)
            if n_seed:
                seed_comp_sizes[root] = n_seed
        if len(seed_comp_sizes) > 1:
            print(
                f"  ! seed's {len(seed)} records landed in {len(seed_comp_sizes)} components "
                f"(sizes: {sorted(seed_comp_sizes.values(), reverse=True)}) -- possible under-merge"
            )

        by_id = {r.id: r for r in records}
        for _root, ids in sorted(comps.items(), key=lambda kv: -len(kv[1]))[: args.show]:
            _print_cluster(session, [by_id[i] for i in ids])
    return 0


if __name__ == "__main__":
    sys.exit(main())
