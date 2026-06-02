"""Apply resolution decisions: build canonical Firm + Link rows.

What it does:

  1. Reads `MatchReviewQueue` rows with status in
     {`auto_approved`, `approved`} — i.e. the system-merged pairs
     plus any human-approved-in-review pairs.
  2. Union-finds connected components across `source_record_a_id`
     and `source_record_b_id`. A component is one canonical firm
     with N source-record members.
  3. Source records NOT in any approved match become singleton
     components (one Firm with one Link).
  4. For each component:
       - Pick canonical field values via source-priority precedence
         (preferring more-enriched sources for each field).
       - Insert a `firms` row capturing both raw and normalized
         forms, plus `field_provenance` JSON recording which source
         supplied each field.
       - Insert one `firm_source_record_links` row per member.
       - Update each member `MatchReviewQueue` row with
         `resulting_firm_id`.

The apply step is idempotent and re-runnable: it clears `firms` and
`firm_source_record_links` at the start. Source-record data is
untouched.

CLI:
    uv run python -m legal_sourcing.resolution.apply
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from legal_sourcing.config import get_settings
from legal_sourcing.models import (
    Firm,
    FirmSourceRecord,
    FirmSourceRecordLink,
    MatchReviewQueue,
)
from legal_sourcing.utils.logging import configure_logging, get_logger

log = get_logger(__name__)


# Source precedence: order of preference when picking a canonical
# field value. The first source in this list whose record has a
# non-empty value for the field wins. Tunable per field via
# FIELD_PRECEDENCE_OVERRIDES below.
DEFAULT_SOURCE_PRIORITY: tuple[str, ...] = (
    "martindale",  # richest enrichment; clean firm names
    "findlaw",  # firm-level cards, real website URLs
    "az_bar",  # individual attorneys; firm fields often sparse
)

# Per-field overrides on the default priority. Empty for now —
# defaults work for the current source mix. Add entries when a
# source clearly wins on a specific field.
FIELD_PRECEDENCE_OVERRIDES: dict[str, tuple[str, ...]] = {}


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
# Canonical-field selection


def _priority_for(field: str) -> tuple[str, ...]:
    return FIELD_PRECEDENCE_OVERRIDES.get(field, DEFAULT_SOURCE_PRIORITY)


def _pick(
    field: str,
    members: list[FirmSourceRecord],
) -> tuple[Any, int | None, str | None]:
    """Pick the canonical value for `field` across `members`.

    Returns ``(value, source_record_id, source_name)`` so the caller
    can record provenance. Returns ``(None, None, None)`` if no member
    has a non-empty value.
    """
    by_source: dict[str, FirmSourceRecord] = {m.source: m for m in members}
    for src in _priority_for(field):
        rec = by_source.get(src)
        if rec is None:
            continue
        v = getattr(rec, field, None)
        if v not in (None, "", [], {}):
            return v, rec.id, rec.source
    # Fallback: any non-empty value, take the first.
    for m in members:
        v = getattr(m, field, None)
        if v not in (None, "", [], {}):
            return v, m.id, m.source
    return None, None, None


def _aggregate_attorney_count(members: list[FirmSourceRecord]) -> int | None:
    """Use MAX across sources — FindLaw cards have 0 (firm-level
    cards have no attorneys), Martindale has firm-wide headcount,
    AZ Bar contributes 1 per attorney. The max is the best signal
    we have without a true cross-source attorney unique-ID.
    """
    vals = [m.attorney_count for m in members if m.attorney_count]
    return max(vals) if vals else None


def _union_practice_areas(
    members: list[FirmSourceRecord],
) -> tuple[list[str], list[str], list[str]]:
    """Union practice areas across sources. Returns (raw, matched,
    unmatched) lists with duplicates removed but order roughly
    preserved.
    """
    raw: list[str] = []
    matched: list[str] = []
    unmatched: list[str] = []
    for m in members:
        for k in m.practice_areas_raw or []:
            if k not in raw:
                raw.append(k)
        for k in m.practice_areas_matched or []:
            if k not in matched:
                matched.append(k)
        for k in m.practice_areas_unmatched or []:
            if k not in unmatched:
                unmatched.append(k)
    return raw, matched, unmatched


def _make_field_provenance(
    members: list[FirmSourceRecord],
    chosen: dict[str, tuple[Any, int | None, str | None]],
) -> dict[str, Any]:
    """Build the `firms.field_provenance` JSON. One entry per
    canonical field that was picked, recording which source record
    supplied the value and when.
    """
    written_at = datetime.now(UTC).isoformat()
    prov: dict[str, Any] = {}
    for field, (_value, source_record_id, source) in chosen.items():
        if source_record_id is None:
            continue
        prov[field] = {
            "source_record_id": source_record_id,
            "source": source,
            "written_at": written_at,
        }
    return prov


# ---------------------------------------------------------------------------
# Per-component build


def _build_firm_from_component(
    members: list[FirmSourceRecord],
) -> tuple[Firm, dict[str, Any]]:
    """Construct (but don't persist) a Firm row from a component plus
    provenance JSON. Returns the Firm and the field_provenance dict
    (the Firm holds it on the column too, but we return it separately
    so the caller can log).
    """
    # Per-field picks.
    chosen: dict[str, tuple[Any, int | None, str | None]] = {}
    for field in (
        "name_raw",
        "name_normalized",
        "website_raw",
        "website_normalized",
        "phone_raw",
        "phone_normalized",
        "year_founded",
    ):
        chosen[field] = _pick(field, members)

    attorney_count = _aggregate_attorney_count(members)
    if attorney_count is not None:
        # Provenance for attorney_count: the source that supplied the
        # max value.
        max_source = max(
            (m for m in members if m.attorney_count),
            key=lambda m: m.attorney_count or 0,
        )
        chosen["attorney_count"] = (attorney_count, max_source.id, max_source.source)
    else:
        chosen["attorney_count"] = (None, None, None)

    field_provenance = _make_field_provenance(members, chosen)

    firm = Firm(
        name=chosen["name_raw"][0] or "",
        name_normalized=chosen["name_normalized"][0],
        website=chosen["website_raw"][0],
        website_normalized=chosen["website_normalized"][0],
        phone=chosen["phone_raw"][0],
        phone_normalized=chosen["phone_normalized"][0],
        year_founded=chosen["year_founded"][0],
        attorney_count=chosen["attorney_count"][0],
        field_provenance=field_provenance,
    )
    return firm, field_provenance


# ---------------------------------------------------------------------------
# Main apply


def apply_decisions(
    *,
    statuses: tuple[str, ...] = ("auto_approved", "approved"),
) -> dict[str, int]:
    """Build canonical Firm + Link rows from current resolution state."""
    settings = get_settings()
    configure_logging()

    engine = create_engine(settings.db_url)
    counts = {
        "components": 0,
        "singletons": 0,
        "multi_member_firms": 0,
        "links": 0,
        "queue_rows_consumed": 0,
        "queue_rows_linked": 0,
    }
    with Session(engine) as session:
        # 1) Clear existing canonical rows. This is the only place
        # firms / firm_source_record_links are written; clearing is
        # safe and makes the apply step idempotent.
        n_links = session.execute(delete(FirmSourceRecordLink)).rowcount
        n_firms = session.execute(delete(Firm)).rowcount
        log.info("apply.cleared", firms=n_firms, links=n_links)

        # 2) Build union-find from the approved match-queue rows.
        approved = session.scalars(
            select(MatchReviewQueue).where(MatchReviewQueue.status.in_(statuses))
        ).all()
        counts["queue_rows_consumed"] = len(approved)
        uf = _UnionFind()

        # Make sure every source-record id ends up in UF (singletons too).
        all_ids = [r.id for r in session.scalars(select(FirmSourceRecord)).all()]
        for rid in all_ids:
            uf.find(rid)

        for m in approved:
            uf.union(m.source_record_a_id, m.source_record_b_id)

        # 3) Group source-record ids by canonical component.
        components = uf.components(all_ids)
        counts["components"] = len(components)
        log.info("apply.components", count=len(components), total_records=len(all_ids))

        # 4) Build Firm + Links per component.
        member_records = {r.id: r for r in session.scalars(select(FirmSourceRecord)).all()}

        firm_by_root: dict[int, Firm] = {}
        for root, member_ids in components.items():
            members = [member_records[i] for i in member_ids]
            if len(members) == 1:
                counts["singletons"] += 1
            else:
                counts["multi_member_firms"] += 1
            firm, _prov = _build_firm_from_component(members)
            session.add(firm)
            firm_by_root[root] = firm
        session.flush()  # populate firm.id values

        # 5) Insert FirmSourceRecordLink rows.
        for root, member_ids in components.items():
            firm = firm_by_root[root]
            for rid in member_ids:
                session.add(
                    FirmSourceRecordLink(
                        firm_id=firm.id,
                        firm_source_record_id=rid,
                        link_method="auto",
                    )
                )
                counts["links"] += 1

        session.flush()

        # 6) Update MatchReviewQueue.resulting_firm_id where both
        # sides ended up in the same component (always true for
        # approved rows, but explicit is better).
        for m in approved:
            root_a = uf.find(m.source_record_a_id)
            root_b = uf.find(m.source_record_b_id)
            if root_a == root_b:
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
    args = parser.parse_args()
    statuses: tuple[str, ...] = (
        ("approved",) if args.include_pending_approved_only else ("auto_approved", "approved")
    )
    counts = apply_decisions(statuses=statuses)
    print(
        f"Apply complete:\n"
        f"  match-queue rows consumed: {counts['queue_rows_consumed']}\n"
        f"  canonical firms          : {counts['components']}\n"
        f"    multi-member           : {counts['multi_member_firms']}\n"
        f"    singletons             : {counts['singletons']}\n"
        f"  firm_source_record_links : {counts['links']}\n"
        f"  match-queue rows tagged with resulting_firm_id: {counts['queue_rows_linked']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
