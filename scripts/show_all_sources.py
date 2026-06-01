"""Cross-source summary across all three pilots."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from legal_sourcing.config import get_settings  # noqa: E402
from legal_sourcing.models import FirmSourceRecord  # noqa: E402


def main() -> int:
    e = create_engine(get_settings().db_url)
    with Session(e) as s:
        sources = s.scalars(select(FirmSourceRecord.source).distinct()).all()
        print(f"{'source':<12} {'firms':>6}  {'multi':>6}  {'website':>8}  {'phone':>6}")
        print("-" * 50)
        for src in sorted(sources):
            n = s.scalar(
                select(func.count()).select_from(FirmSourceRecord).where(
                    FirmSourceRecord.source == src
                )
            )
            multi = s.scalar(
                select(func.count()).select_from(FirmSourceRecord).where(
                    FirmSourceRecord.source == src,
                    FirmSourceRecord.attorney_count > 1,
                )
            )
            web = s.scalar(
                select(func.count()).select_from(FirmSourceRecord).where(
                    FirmSourceRecord.source == src,
                    FirmSourceRecord.website_raw.is_not(None),
                )
            )
            ph = s.scalar(
                select(func.count()).select_from(FirmSourceRecord).where(
                    FirmSourceRecord.source == src,
                    FirmSourceRecord.phone_raw.is_not(None),
                )
            )
            print(f"{src:<12} {n:>6}  {multi:>6}  {web:>8}  {ph:>6}")
        total = s.scalar(select(func.count()).select_from(FirmSourceRecord))
        print(f"{'TOTAL':<12} {total:>6}")

        # Cross-source name overlap probe — same normalized name appearing
        # in 2+ sources. Coarse, just a teaser for M6 resolution.
        rows = s.scalars(
            select(FirmSourceRecord).where(FirmSourceRecord.name_normalized.is_not(None))
        ).all()
        by_name: dict[str, set[str]] = {}
        for r in rows:
            by_name.setdefault(r.name_normalized, set()).add(r.source)
        cross = {n: sources for n, sources in by_name.items() if len(sources) >= 2}
        print()
        print(f"Firms whose normalized name appears in 2+ sources: {len(cross)}")
        # Top 10 by source count + sample.
        cross_sorted = sorted(cross.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        for name, src_set in cross_sorted[:10]:
            print(f"  {name:50s} {sorted(src_set)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
