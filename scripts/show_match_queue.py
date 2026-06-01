"""Inspect resolution decisions. Shows sample pairs from each band
(auto-approved / pending / rejected) so the user can tune thresholds.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from legal_sourcing.config import get_settings  # noqa: E402
from legal_sourcing.models import FirmSourceRecord, MatchReviewQueue  # noqa: E402


SAMPLES_PER_BAND = 8


def main() -> int:
    e = create_engine(get_settings().db_url)
    with Session(e) as s:
        total = s.scalar(select(func.count()).select_from(MatchReviewQueue))
        print(f"== MatchReviewQueue: {total} rows ==\n")
        # Per-status counts.
        rows_by_status = s.execute(
            select(
                MatchReviewQueue.status,
                func.count(),
                func.min(MatchReviewQueue.score_total),
                func.max(MatchReviewQueue.score_total),
                func.avg(MatchReviewQueue.score_total),
            ).group_by(MatchReviewQueue.status)
        ).all()
        for status, count, lo, hi, avg in rows_by_status:
            print(f"  {status:<14s}  n={count:<5d}  score: min={lo:.1f} max={hi:.1f} avg={avg:.1f}")
        print()

        for band, status, label in (
            ("auto_approved", "auto_approved", "AUTO-APPROVED"),
            ("pending", "pending", "PENDING REVIEW"),
            ("rejected", "rejected", "REJECTED (stored)"),
        ):
            print(f"\n=== {label} (sample {SAMPLES_PER_BAND}) ===")
            samples = s.scalars(
                select(MatchReviewQueue)
                .where(MatchReviewQueue.status == status)
                .order_by(MatchReviewQueue.score_total.desc())
                .limit(SAMPLES_PER_BAND)
            ).all()
            if not samples:
                print("  (no rows)")
                continue
            for m in samples:
                a = s.get(FirmSourceRecord, m.source_record_a_id)
                b = s.get(FirmSourceRecord, m.source_record_b_id)
                print(
                    f"\n  score={m.score_total}  "
                    f"[{a.source}]{a.name_raw[:42] if a.name_raw else '<no name>':42s} ({a.primary_city}, {a.primary_state})  "
                    f"\n  {' ' * 6}"
                    f"[{b.source}]{b.name_raw[:42] if b.name_raw else '<no name>':42s} ({b.primary_city}, {b.primary_state})"
                )
                comps = m.score_components or {}
                parts = []
                for k, v in comps.items():
                    if v is None:
                        parts.append(f"{k}=-")
                    else:
                        parts.append(f"{k}={v:.2f}")
                print(f"        components: {', '.join(parts)}")

        # Cross-source match summary.
        print("\n=== Cross-source matches (auto_approved + pending) ===")
        cross_q = s.execute(
            select(
                FirmSourceRecord.source.label("a_source"),
                FirmSourceRecord.source.label("b_source"),
            )
        )  # placeholder
        # Cleaner approach: iterate.
        from collections import Counter

        pair_sources: Counter = Counter()
        for m in s.scalars(
            select(MatchReviewQueue).where(
                MatchReviewQueue.status.in_(["auto_approved", "pending"])
            )
        ):
            a = s.get(FirmSourceRecord, m.source_record_a_id)
            b = s.get(FirmSourceRecord, m.source_record_b_id)
            key = tuple(sorted([a.source, b.source]))
            pair_sources[key] += 1
        for sources, n in pair_sources.most_common():
            print(f"  {sources[0]:<12s} <-> {sources[1]:<12s} : {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
