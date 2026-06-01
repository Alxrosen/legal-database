"""Backfill primary_city / primary_state / primary_postal_code on
every FirmSourceRecord from the existing offices JSON.

No re-scrape — pulls the primary office's normalized city/state/zip
from offices[i where is_primary=True] (or offices[0] as a fallback)
and writes the denormalized columns. Idempotent.
"""

from __future__ import annotations

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
    updated = 0
    with Session(engine) as s:
        for r in s.scalars(select(FirmSourceRecord)).all():
            offices = r.offices or []
            if not offices:
                continue
            primary = next(
                (o for o in offices if o.get("is_primary")), offices[0]
            )
            # Prefer the normalized sub-dict; fall back to raw fields.
            norm = primary.get("normalized") or {}
            city = norm.get("city") or (primary.get("city_raw") or "").strip() or None
            state_raw = (
                norm.get("state")
                or (primary.get("state_raw") or "").strip().upper()
            )
            state = (state_raw or "")[:8] or None
            zipc = (
                norm.get("postal_code")
                or (primary.get("postal_code_raw") or "").strip().split("-")[0]
                or None
            )
            zipc = zipc[:16] if zipc else None
            new_city = city
            new_state = state
            new_zip = zipc
            if (
                r.primary_city != new_city
                or r.primary_state != new_state
                or r.primary_postal_code != new_zip
            ):
                r.primary_city = new_city
                r.primary_state = new_state
                r.primary_postal_code = new_zip
                updated += 1
        s.commit()
    print(f"Backfilled primary_* on {updated} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
