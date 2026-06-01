"""Inspect FindLaw FirmSourceRecord rows after the pilot."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from legal_sourcing.config import get_settings  # noqa: E402
from legal_sourcing.models import FirmSourceRecord  # noqa: E402


def main() -> int:
    e = create_engine(get_settings().db_url)
    with Session(e) as s:
        rows = s.scalars(
            select(FirmSourceRecord).where(FirmSourceRecord.source == "findlaw")
        ).all()

    print(f"Total FindLaw firms : {len(rows)}")
    print(f"  with website      : {sum(1 for r in rows if r.website_raw)}")
    print(f"  with phone        : {sum(1 for r in rows if r.phone_raw)}")
    print(
        f"  with full address : "
        f"{sum(1 for r in rows if r.offices and r.offices[0].get('postal_code_raw'))}"
    )

    # By state
    by_state = Counter((r.additional_data or {}).get("state_slug") for r in rows)
    print(f"  by state          : {dict(by_state)}")

    # Practice-area overlap — how many areas did each firm appear under?
    overlap = Counter()
    pa_counter: Counter = Counter()
    for r in rows:
        pas = set(r.practice_areas_raw or [])
        overlap[len(pas)] += 1
        for pa in pas:
            pa_counter[pa] += 1
    print()
    print("Practice-area overlap (count of distinct slugs per firm):")
    for k in sorted(overlap):
        print(f"  {k:2d} slug{'s' if k != 1 else ''}: {overlap[k]} firm{'s' if overlap[k] != 1 else ''}")
    print()
    print("Practice-area volume (firms tagged per area):")
    for pa, n in pa_counter.most_common():
        print(f"  {pa:36s} {n}")

    # Top firms by practice-area breadth — likely PI specialists.
    print()
    print("Top 10 firms by number of PI practice-area slugs:")
    by_breadth = sorted(rows, key=lambda r: -len(set(r.practice_areas_raw or [])))
    for r in by_breadth[:10]:
        slugs = sorted(set(r.practice_areas_raw or []))
        city = (r.additional_data or {}).get("city_slug")
        state = (r.additional_data or {}).get("state_slug")
        site = (r.website_raw or "")[:40]
        print(f"  {len(slugs):2d}  {r.name_raw:48s}  [{city}, {state}]  {site}")
        print(f"         {slugs}")

    # Sample firm detail
    if rows:
        r = by_breadth[0]
        print()
        print("Spotlight: firm with the broadest practice-area coverage")
        print(f"  name_raw           : {r.name_raw!r}")
        print(f"  name_normalized    : {r.name_normalized!r}")
        print(f"  website_raw        : {r.website_raw!r}")
        print(f"  website_normalized : {r.website_normalized!r}")
        print(f"  phone_normalized   : {r.phone_normalized!r}")
        print(f"  practice_areas_raw : {r.practice_areas_raw}")
        print(f"  practice_areas_matched : {r.practice_areas_matched}")
        print(f"  practice_areas_unmatched : {r.practice_areas_unmatched}")
        print(f"  offices            : {r.offices}")
        print(f"  additional_data    : {r.additional_data}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
