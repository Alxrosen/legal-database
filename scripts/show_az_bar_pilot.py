"""Read the FirmSourceRecord rows ingested by the AZ Bar pilot and
print a compact summary."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from legal_sourcing.config import get_settings  # noqa: E402
from legal_sourcing.models import FirmSourceRecord  # noqa: E402


def main() -> int:
    engine = create_engine(get_settings().db_url)
    with Session(engine) as session:
        rows = session.scalars(
            select(FirmSourceRecord)
            .where(FirmSourceRecord.source == "az_bar")
            .order_by(FirmSourceRecord.name_normalized)
        ).all()

    print(f"\n== {len(rows)} az_bar FirmSourceRecord rows ==\n")
    print(f"{'#':>3}  {'firm name':<48} {'attys':>5}  {'PA matched'}")
    print("-" * 110)
    for i, r in enumerate(rows, 1):
        name = (r.name_raw or "<no name>")[:48]
        attys = r.attorney_count or 0
        pa = ", ".join(r.practice_areas_matched or [])[:60]
        print(f"{i:>3}  {name:<48} {attys:>5}  {pa}")

    # Spotlight: one detailed row to show what's stored end-to-end.
    if rows:
        print("\n== Spotlight: row #1 in detail ==\n")
        r = rows[0]
        print(f"name_raw            : {r.name_raw!r}")
        print(f"name_normalized     : {r.name_normalized!r}")
        print(f"website_raw         : {r.website_raw!r}")
        print(f"website_normalized  : {r.website_normalized!r}")
        print(f"phone_raw           : {r.phone_raw!r}")
        print(f"phone_normalized    : {r.phone_normalized!r}")
        print(f"attorney_count      : {r.attorney_count}")
        print(f"source              : {r.source}")
        print(f"source_firm_id      : {r.source_firm_id}")
        print(f"source_url          : {r.source_url}")
        print(f"raw_payload_path    : {r.raw_payload_path}")
        print(f"http_status         : {r.http_status}")
        print(f"scraped_at          : {r.scraped_at}")
        print(f"practice_areas_raw     : {r.practice_areas_raw[:5]}{'...' if len(r.practice_areas_raw)>5 else ''} (n={len(r.practice_areas_raw)})")
        print(f"practice_areas_matched : {r.practice_areas_matched}")
        print(f"practice_areas_unmatched (sample): {r.practice_areas_unmatched[:5]}{'...' if len(r.practice_areas_unmatched)>5 else ''} (n={len(r.practice_areas_unmatched)})")
        print(f"contacts (n={len(r.contacts)}):")
        for c in r.contacts:
            print(
                f"    - {c.get('name_raw'):<32} bar={c.get('bar_number')}  "
                f"status={c.get('member_status')!s:<10} title_rank=None"
            )
        print(f"offices (n={len(r.offices)}):")
        for o in r.offices:
            norm = o.get("normalized") or {}
            print(
                f"    - {o.get('street_raw')!s:<40} | "
                f"{norm.get('city')!s:<16} {norm.get('state')!s:<3} "
                f"{norm.get('postal_code')!s}"
            )
        print(f"additional_data keys: {sorted((r.additional_data or {}).keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
