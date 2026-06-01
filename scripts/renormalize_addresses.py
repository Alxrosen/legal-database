"""Re-run the office-address normalization on every existing
FirmSourceRecord. No re-scrape — uses the raw fields already stored
in the `offices` JSON column.

Use case: after fixing a bug in the address normalizer, every row's
`offices[i].normalized` sub-dict can be regenerated from the raw
fields without re-fetching anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import copy  # noqa: E402

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from legal_sourcing.config import get_settings  # noqa: E402
from legal_sourcing.models import FirmSourceRecord  # noqa: E402
from legal_sourcing.pipelines.scrape_az_bar import normalize_record  # noqa: E402


def main() -> int:
    engine = create_engine(get_settings().db_url)
    updated = 0
    with Session(engine) as session:
        rows = session.scalars(select(FirmSourceRecord)).all()
        for r in rows:
            # Deep-copy the JSON columns into a scratch dict so the
            # normalizer mutates fresh objects. Otherwise it would
            # mutate the same dicts the ORM is tracking, and
            # SQLAlchemy wouldn't see a "different" value on
            # assignment.
            scratch = {
                "name_raw": r.name_raw,
                "website_raw": r.website_raw,
                "phone_raw": r.phone_raw,
                "contacts": copy.deepcopy(r.contacts or []),
                "offices": copy.deepcopy(r.offices or []),
                "practice_areas_raw": list(r.practice_areas_raw or []),
            }
            normalize_record(scratch)
            r.name_normalized = scratch.get("name_normalized")
            r.website_normalized = scratch.get("website_normalized")
            r.phone_normalized = scratch.get("phone_normalized")
            r.contacts = scratch["contacts"]
            r.offices = scratch["offices"]
            r.practice_areas_matched = scratch.get("practice_areas_matched") or []
            r.practice_areas_unmatched = scratch.get("practice_areas_unmatched") or []
            # JSON columns aren't auto-tracked by SQLAlchemy when the
            # value is mutated in place (and deep-copy + reassign
            # doesn't always trip the change-detection either). Flag
            # them explicitly so the UPDATE fires.
            for col in (
                "contacts",
                "offices",
                "practice_areas_matched",
                "practice_areas_unmatched",
            ):
                flag_modified(r, col)
            updated += 1
        session.commit()
    print(f"Re-normalized {updated} FirmSourceRecord rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
