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

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from legal_sourcing.db import make_engine  # noqa: E402
from legal_sourcing.models import FirmSourceRecord  # noqa: E402


def main() -> int:
    engine = make_engine()
    # Stream IDs first, then update in bounded chunks so each transaction holds
    # the (whole-DB) SQLite write lock only briefly — safe to run concurrently
    # with the live Martindale scrape (busy_timeout=30s via make_engine).
    # Idempotent: only changed rows are written.
    with Session(engine) as s:
        ids = list(s.scalars(select(FirmSourceRecord.id)).all())
    updated = 0
    chunk_size = 5000
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start : start + chunk_size]
        with Session(engine) as s:
            for r in s.scalars(
                select(FirmSourceRecord).where(FirmSourceRecord.id.in_(chunk))
            ):
                offices = r.offices or []
                if not offices:
                    continue
                primary = next((o for o in offices if o.get("is_primary")), offices[0])
                # Prefer the normalized sub-dict; fall back to raw fields.
                norm = primary.get("normalized") or {}
                city = norm.get("city") or (primary.get("city_raw") or "").strip() or None
                state_raw = norm.get("state") or (primary.get("state_raw") or "").strip().upper()
                state = (state_raw or "")[:8] or None
                zipc = (
                    norm.get("postal_code")
                    or (primary.get("postal_code_raw") or "").strip().split("-")[0]
                    or None
                )
                zipc = zipc[:16] if zipc else None
                if (
                    r.primary_city != city
                    or r.primary_state != state
                    or r.primary_postal_code != zipc
                ):
                    r.primary_city = city
                    r.primary_state = state
                    r.primary_postal_code = zipc
                    updated += 1
            s.commit()
    print(f"Backfilled primary_* on {updated} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
