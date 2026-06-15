"""Apply resolution decisions: build canonical Firm + Link rows.

What it does:

  1. Reads `MatchReviewQueue` rows with status in {`auto_approved`,
     `approved`} — the system-merged pairs plus any human-approved ones.
  2. Union-finds connected components across `source_record_a_id` /
     `source_record_b_id`. Each component is one canonical firm with N
     source-record members; records in no approved pair are singletons.
  3. For each component, fuses its member source records (plus the matching
     `WebsiteEnrichment` row, joined on the cluster's website) into one
     canonical `Firm` via :func:`resolution.fusion.fuse_cluster` — a
     reliability- and recency-weighted, field-by-field truth-discovery vote
     (see resolution/fusion.py). Writes the `Firm` (with `field_provenance`),
     one `firm_source_record_links` row per member, and stamps each consumed
     `MatchReviewQueue` row with `resulting_firm_id`.

The apply step is idempotent and re-runnable: it clears `firms` and
`firm_source_record_links` at the start. Source-record data is untouched.

CLI:
    uv run python -m legal_sourcing.resolution.apply
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from legal_sourcing.db import make_engine
from legal_sourcing.models import (
    Firm,
    FirmSourceRecord,
    FirmSourceRecordLink,
    MatchReviewQueue,
    WebsiteEnrichment,
)
from legal_sourcing.resolution.fusion import _aggregate_attorney_count, fuse_cluster
from legal_sourcing.resolution.identity import is_firm_name, is_identity_website
from legal_sourcing.utils.logging import configure_logging, get_logger

log = get_logger(__name__)

# Re-exported from their original home for callers/tests that still import them
# here. The implementations now live in resolution.fusion / resolution.identity.
_looks_like_firm = is_firm_name

__all__ = [
    "_UnionFind",
    "_aggregate_attorney_count",
    "_looks_like_firm",
    "apply_decisions",
    "main",
]


# ---------------------------------------------------------------------------
# Union-find


class _UnionFind:
    """Compact union-find over arbitrary hashable keys."""

    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        if x not in self._parent:
            self._parent[x] = x
            return x
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # path compression
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self._parent[rx] = ry

    def components(self, all_keys: Iterable[int]) -> dict[int, list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for k in all_keys:
            out[self.find(k)].append(k)
        return out


# ---------------------------------------------------------------------------
# Website-enrichment join


def _enrichment_for(
    members: list[FirmSourceRecord],
    enrichment_by_website: dict[str, WebsiteEnrichment],
) -> WebsiteEnrichment | None:
    """The WebsiteEnrichment row for the cluster's dominant identity website
    (the firm's own domain), or None."""
    webs = [
        m.website_normalized
        for m in members
        if m.website_normalized and is_identity_website(m.website_normalized)
    ]
    if not webs:
        return None
    web = Counter(webs).most_common(1)[0][0]
    return enrichment_by_website.get(web)


def _is_identified(rec: FirmSourceRecord) -> bool:
    """A record carries firm identity if it has any of name / phone / website.
    Records with none (≈55% of the corpus — attorney rows whose firm-level
    fields are empty) can't be a distinct firm; as singletons they'd be
    nameless junk, so apply skips them by default (source data is untouched)."""
    return bool(
        (rec.name_raw or "").strip()
        or (rec.phone_normalized or "").strip()
        or (rec.website_normalized or "").strip()
    )


# ---------------------------------------------------------------------------
# Main apply


def _materialize_firms(
    session: Session,
    components: dict[int, list[int]],
    counts: dict[str, int],
    *,
    skip_unidentified: bool,
) -> dict[int, Firm]:
    """Fuse each component into a canonical Firm + write its links. Mutates
    ``counts`` (singletons / multi_member_firms / skipped_unidentified / links).
    Returns root -> Firm. Shared by both the queue-based and cluster-based apply."""
    member_records = {r.id: r for r in session.scalars(select(FirmSourceRecord)).all()}
    enrichment_by_website = {e.website: e for e in session.scalars(select(WebsiteEnrichment)).all()}

    firm_by_root: dict[int, Firm] = {}
    for root, member_ids in components.items():
        members = [member_records[i] for i in member_ids]
        if len(members) == 1:
            if skip_unidentified and not _is_identified(members[0]):
                counts["skipped_unidentified"] += 1
                continue
            counts["singletons"] += 1
        else:
            counts["multi_member_firms"] += 1
        enrichment = _enrichment_for(members, enrichment_by_website)
        result = fuse_cluster(members, enrichment)
        firm = Firm(
            name=result.name,
            name_normalized=result.name_normalized,
            website=result.website,
            website_normalized=result.website_normalized,
            phone=result.phone,
            phone_normalized=result.phone_normalized,
            year_founded=result.year_founded,
            attorney_count=result.attorney_count,
            city=result.city,
            state=result.state,
            practice_areas=result.practice_areas,
            field_provenance=result.field_provenance,
        )
        session.add(firm)
        firm_by_root[root] = firm
    session.flush()  # populate firm.id values

    for root, member_ids in components.items():
        firm = firm_by_root.get(root)
        if firm is None:
            continue
        for rid in member_ids:
            session.add(
                FirmSourceRecordLink(firm_id=firm.id, firm_source_record_id=rid, link_method="auto")
            )
            counts["links"] += 1
    session.flush()
    return firm_by_root


def apply_clusters(
    components: dict[int, list[int]], *, skip_unidentified: bool = True
) -> dict[str, int]:
    """Build the canonical Firm + Link tables from an EXTERNALLY-computed clustering
    (record id -> component), e.g. the Splink + backstop pipeline
    (``dry_run.compute_backstop_clusters``). Clearing keeps it idempotent; source
    rows are never touched. This is the canonical write for the Splink pivot —
    ``fuse_cluster`` (survivorship) and ``identity.py`` are unchanged."""
    configure_logging()
    engine = make_engine()
    counts = {
        "components": len(components),
        "singletons": 0,
        "multi_member_firms": 0,
        "skipped_unidentified": 0,
        "links": 0,
    }
    with Session(engine) as session:
        n_links = session.execute(delete(FirmSourceRecordLink)).rowcount
        n_firms = session.execute(delete(Firm)).rowcount
        log.info("apply.cleared", firms=n_firms, links=n_links)
        _materialize_firms(session, components, counts, skip_unidentified=skip_unidentified)
        session.commit()
        log.info("apply.clusters.done", **counts)
    return counts


def apply_decisions(
    *,
    statuses: tuple[str, ...] = ("auto_approved", "approved"),
    skip_unidentified: bool = True,
) -> dict[str, int]:
    """Build canonical Firm + Link rows from current resolution state.

    Singleton components whose one record has no name/phone/website are skipped
    by default (they can't be a usable firm); pass ``skip_unidentified=False``
    to materialize them anyway.
    """
    configure_logging()

    engine = make_engine()
    counts = {
        "components": 0,
        "singletons": 0,
        "multi_member_firms": 0,
        "skipped_unidentified": 0,
        "links": 0,
        "queue_rows_consumed": 0,
        "queue_rows_linked": 0,
    }
    with Session(engine) as session:
        # 1) Clear existing canonical rows (the only place firms /
        # firm_source_record_links are written; clearing keeps apply idempotent).
        n_links = session.execute(delete(FirmSourceRecordLink)).rowcount
        n_firms = session.execute(delete(Firm)).rowcount
        log.info("apply.cleared", firms=n_firms, links=n_links)

        # 2) Union-find from the approved match-queue rows.
        approved = session.scalars(
            select(MatchReviewQueue).where(MatchReviewQueue.status.in_(statuses))
        ).all()
        counts["queue_rows_consumed"] = len(approved)
        uf = _UnionFind()
        all_ids = list(session.scalars(select(FirmSourceRecord.id)).all())
        for rid in all_ids:
            uf.find(rid)
        for m in approved:
            uf.union(m.source_record_a_id, m.source_record_b_id)

        # 3) Group source-record ids by canonical component.
        components = uf.components(all_ids)
        counts["components"] = len(components)
        log.info("apply.components", count=len(components), total_records=len(all_ids))

        # 4-5) Fuse + write firms/links.
        firm_by_root = _materialize_firms(
            session, components, counts, skip_unidentified=skip_unidentified
        )

        # 6) Stamp MatchReviewQueue.resulting_firm_id for approved rows.
        for m in approved:
            root_a = uf.find(m.source_record_a_id)
            root_b = uf.find(m.source_record_b_id)
            if root_a == root_b and root_a in firm_by_root:
                m.resulting_firm_id = firm_by_root[root_a].id
                counts["queue_rows_linked"] += 1
        session.commit()
        log.info("apply.done", **counts)
        return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-pending-approved-only",
        action="store_true",
        help="If set, only consume MatchReviewQueue rows with "
        "status='approved' (skip auto_approved). Useful for re-running "
        "apply against a queue that's been hand-reviewed.",
    )
    parser.add_argument(
        "--keep-unidentified",
        action="store_true",
        help="Also materialize singleton firms with no name/phone/website "
        "(skipped by default as they can't be a usable firm).",
    )
    parser.add_argument(
        "--splink",
        action="store_true",
        help="Use the Splink + backstop clustering (the pivot engine) instead of "
        "the match_review_queue. Computes clusters then writes canonical firms.",
    )
    parser.add_argument("--threshold", type=float, default=0.5, help="Splink clustering threshold.")
    parser.add_argument(
        "--must-link",
        action="store_true",
        help="With --splink: also run the website MUST-LINK recall pass (recovers "
        "shared-identity-domain false negatives; mis-attribution + generic-domain guarded).",
    )
    args = parser.parse_args()

    if args.splink:
        # Lazy import so core apply stays splink-free unless this path is used.
        from legal_sourcing.resolution.dry_run import compute_backstop_clusters

        ml = " + must-link" if args.must_link else ""
        print(f"computing Splink + backstop{ml} clusters (threshold {args.threshold}) ...")
        with Session(make_engine()) as s:
            components = compute_backstop_clusters(s, args.threshold, must_link=args.must_link)
        counts = apply_clusters(components, skip_unidentified=not args.keep_unidentified)
        firms_created = counts["multi_member_firms"] + counts["singletons"]
        print(
            f"Canonical DB created (Splink + backstop):\n"
            f"  source records clustered : {counts['components']:,}\n"
            f"  canonical firms created  : {firms_created:,}\n"
            f"    multi-member           : {counts['multi_member_firms']:,}\n"
            f"    singletons             : {counts['singletons']:,}\n"
            f"  skipped (unidentified)   : {counts['skipped_unidentified']:,}\n"
            f"  firm_source_record_links : {counts['links']:,}"
        )
        return 0

    statuses: tuple[str, ...] = (
        ("approved",) if args.include_pending_approved_only else ("auto_approved", "approved")
    )
    counts = apply_decisions(statuses=statuses, skip_unidentified=not args.keep_unidentified)
    firms_created = counts["multi_member_firms"] + counts["singletons"]
    print(
        f"Apply complete:\n"
        f"  match-queue rows consumed: {counts['queue_rows_consumed']}\n"
        f"  components (total)       : {counts['components']}\n"
        f"  canonical firms created  : {firms_created}\n"
        f"    multi-member           : {counts['multi_member_firms']}\n"
        f"    singletons             : {counts['singletons']}\n"
        f"  skipped (unidentified)   : {counts['skipped_unidentified']}\n"
        f"  firm_source_record_links : {counts['links']}\n"
        f"  match-queue rows tagged with resulting_firm_id: {counts['queue_rows_linked']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
