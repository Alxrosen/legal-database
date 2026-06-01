"""Show the enriched fields for the Martindale firm-profile pass."""

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
        martindale_n = s.scalar(
            select(func.count()).select_from(FirmSourceRecord).where(
                FirmSourceRecord.source == "martindale"
            )
        )
        by_status = s.execute(
            select(FirmSourceRecord.enrichment_status, func.count())
            .where(FirmSourceRecord.source == "martindale")
            .group_by(FirmSourceRecord.enrichment_status)
        ).all()
        print(f"Martindale rows                : {martindale_n}")
        for status, n in by_status:
            print(f"  enrichment_status={status!s:14s} : {n}")

        enriched = s.scalars(
            select(FirmSourceRecord)
            .where(FirmSourceRecord.source == "martindale")
            .where(FirmSourceRecord.enrichment_status == "enriched")
        ).all()
        sub = s.scalar(
            select(func.count()).select_from(FirmSourceRecord).where(
                FirmSourceRecord.is_subscriber == True  # noqa: E712
            )
        )
        print(f"  is_subscriber=true total      : {sub}")
        print(f"  with year_founded             : {sum(1 for r in enriched if r.year_founded)}")
        print(f"  with firm_short_description   : {sum(1 for r in enriched if r.firm_short_description)}")
        print(f"  with firm_descriptions        : {sum(1 for r in enriched if r.firm_descriptions)}")
        print(f"  with office_count             : {sum(1 for r in enriched if r.office_count is not None)}")
        # primary_postal_code coverage delta from enrichment
        with_zip = sum(1 for r in enriched if r.primary_postal_code)
        print(f"  with primary_postal_code      : {with_zip} / {len(enriched)}")

        # Top firms by attorney_count for spot check
        print()
        print("Top 5 enriched firms by attorney_count:")
        top = sorted(enriched, key=lambda r: -(r.attorney_count or 0))[:5]
        for r in top:
            print(
                f"  {r.name_raw[:42]:42s} | "
                f"attys={r.attorney_count} | "
                f"year={r.year_founded} | "
                f"city/state={r.primary_city}, {r.primary_state} {r.primary_postal_code or ''} | "
                f"PAs={len(r.practice_areas_raw or [])}"
            )

        # Spotlight: Prim & Mendheim
        print()
        prim = next((r for r in enriched if r.name_raw and r.name_raw.startswith("Prim")), None)
        if prim is not None:
            print("Spotlight: Prim & Mendheim (the user's example)")
            print(f"  name_raw              : {prim.name_raw}")
            print(f"  primary_city          : {prim.primary_city}")
            print(f"  primary_state         : {prim.primary_state}")
            print(f"  primary_postal_code   : {prim.primary_postal_code}")
            print(f"  year_founded          : {prim.year_founded}")
            print(f"  office_count          : {prim.office_count}")
            print(f"  attorney_count        : {prim.attorney_count}")
            print(f"  is_subscriber         : {prim.is_subscriber}")
            print(f"  firm_short_description: {prim.firm_short_description!r}")
            print(f"  practice_areas_raw    : {prim.practice_areas_raw}")
            print(f"  practice_areas_matched: {prim.practice_areas_matched}")
            print(f"  practice_areas_unmatched: {prim.practice_areas_unmatched}")
            print(f"  firm_descriptions     :")
            for d in prim.firm_descriptions or []:
                print(f"    [{d.get('heading')!r}]  {(d.get('text') or '')[:120]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
