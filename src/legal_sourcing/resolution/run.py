"""End-to-end resolution: blocking -> scoring -> MatchReviewQueue.

CLI:
    uv run python -m legal_sourcing.resolution.run resolve

What it does:

  1. Clears existing rows in `match_review_queue`. The table holds
     decision state for the CURRENT scoring + threshold config; we
     re-derive on every run because tuning thresholds without
     re-running would leave stale rows in the queue.
  2. Loads all FirmSourceRecord rows.
  3. Generates candidate pairs via multi-key blocking.
  4. Scores each pair via `resolution.scoring.score_pair`.
  5. Assigns a status per `DEFAULT_THRESHOLDS`:
       - >= 85  -> `auto_approved`
       - 60-84  -> `pending`  (queued for human review)
       - 40-59  -> `rejected` (auto-rejected; below review cutoff)
       - < 40   -> dropped (not stored — clearly not a match)
  6. Inserts each surviving pair into `match_review_queue` with the
     full score_components dict and a snapshot of the thresholds.

Reasons we store rejected pairs (40-59): they're the "almost matched"
band. When we tune thresholds DOWN later, we can re-decide without
re-scoring. Pairs below 40 are clearly different firms — storing them
would balloon the table for no benefit.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from legal_sourcing.db import make_engine
from legal_sourcing.models import FirmSourceRecord, MatchReviewQueue
from legal_sourcing.resolution.blocking import (
    bucket_stats,
    generate_candidate_pairs,
)
from legal_sourcing.resolution.scoring import DEFAULT_WEIGHTS, score_pair
from legal_sourcing.utils.logging import configure_logging, get_logger

log = get_logger(__name__)


DEFAULT_THRESHOLDS: dict[str, float] = {
    "auto_merge": 85.0,
    "review": 60.0,
    "store_min": 40.0,
}


def resolve_all(
    *,
    thresholds: dict[str, float] | None = None,
    weights: dict[str, float] | None = None,
    clear_queue: bool = True,
) -> dict[str, int]:
    configure_logging()
    th = thresholds or DEFAULT_THRESHOLDS
    w = weights or DEFAULT_WEIGHTS

    engine = make_engine()
    counts = {
        "candidate_pairs": 0,
        "auto_approved": 0,
        "pending": 0,
        "rejected": 0,
        "dropped": 0,
    }
    with Session(engine) as session:
        if clear_queue:
            n_cleared = session.execute(delete(MatchReviewQueue)).rowcount
            session.commit()
            log.info("resolution.queue_cleared", count=n_cleared)

        records = session.scalars(select(FirmSourceRecord)).all()
        by_id = {r.id: r for r in records}
        log.info("resolution.loaded_records", count=len(records))

        # Diagnostic: how big are the blocking buckets?
        stats = bucket_stats(records)
        log.info("resolution.bucket_stats", **stats)

        pairs_iter = generate_candidate_pairs(records)
        new_rows: list[MatchReviewQueue] = []
        for a_id, b_id in pairs_iter:
            counts["candidate_pairs"] += 1
            score = score_pair(by_id[a_id], by_id[b_id], weights=w)
            total = score["total"]
            if total >= th["auto_merge"]:
                status = "auto_approved"
                counts["auto_approved"] += 1
            elif total >= th["review"]:
                status = "pending"
                counts["pending"] += 1
            elif total >= th["store_min"]:
                status = "rejected"
                counts["rejected"] += 1
            else:
                counts["dropped"] += 1
                continue

            new_rows.append(
                MatchReviewQueue(
                    source_record_a_id=a_id,
                    source_record_b_id=b_id,
                    score_total=total,
                    score_components=score["components"],
                    thresholds=dict(th),
                    status=status,
                )
            )
        # Batch insert.
        if new_rows:
            session.bulk_save_objects(new_rows)
            session.commit()
        log.info("resolution.done", **counts)
        return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("resolve",))
    parser.add_argument(
        "--auto-merge",
        type=float,
        default=DEFAULT_THRESHOLDS["auto_merge"],
        help="Auto-merge threshold (default 85).",
    )
    parser.add_argument(
        "--review",
        type=float,
        default=DEFAULT_THRESHOLDS["review"],
        help="Review threshold lower bound (default 60).",
    )
    parser.add_argument(
        "--store-min",
        type=float,
        default=DEFAULT_THRESHOLDS["store_min"],
        help="Minimum score to store at all (default 40).",
    )
    args = parser.parse_args()
    if args.mode == "resolve":
        th = {
            "auto_merge": args.auto_merge,
            "review": args.review,
            "store_min": args.store_min,
        }
        counts = resolve_all(thresholds=th)
        print(
            f"Resolution complete:\n"
            f"  candidate pairs : {counts['candidate_pairs']}\n"
            f"  auto-approved   : {counts['auto_approved']}\n"
            f"  pending review  : {counts['pending']}\n"
            f"  rejected (stored): {counts['rejected']}\n"
            f"  dropped (<{args.store_min}): {counts['dropped']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
