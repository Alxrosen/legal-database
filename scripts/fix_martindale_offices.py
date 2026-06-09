"""Fix Martindale office addresses where the whole address line was crammed
into ``offices[].city_raw`` with no ``state`` (blocking ``primary_state``).

Root cause (Fixer's lane): Martindale's city-listing cards
(``/all-lawyers/<city>/<state>/``) carry the full office address in one
location element — e.g. ``"101 Court Sq Ste I, Abbeville, AL 36310-2135"``.
``MartindaleCityParser._parse_location_text``'s strict ``"City, ST"`` regex
never matched these, so the string fell into ``city_raw`` and the normalizer
left ``state=None`` on ~76.5k rows. The address is fully recoverable from the
stored value (path a: fix from the DB entry, no re-scrape) via
``legal_sourcing.normalize.address.parse_full_location``.

Modes:
  --backstop   Run the curated input->expected cases and print PASS/FAIL.
  (default)    DRY-RUN: scan the DB, repair in memory, report counts + a
               sample of before/after diffs + residuals. **Writes nothing.**
  --apply      Write the repaired ``offices[].normalized`` back, in 5k-row
               chunks via make_engine() (concurrent-safe with the live scrape).

The repair only touches ``offices[].normalized`` (the derived comparison form);
the verbatim ``city_raw`` is left intact, per the project's raw+normalized
convention.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from legal_sourcing.db import make_engine  # noqa: E402
from legal_sourcing.models import FirmSourceRecord  # noqa: E402
from legal_sourcing.normalize.address import parse_full_location  # noqa: E402

# ---- The back-stop case list (input city_raw -> expected normalized) --------
# Real shapes pulled from the DB plus curated edge cases. Add a row here and
# re-run `--backstop` to confirm the repair handles it.
# Each expected dict: city / state / postal_code (street checked loosely).
BACKSTOP_CASES: list[tuple[str, dict[str, Any]]] = [
    # street, city, ST, zip
    ("101 Court Sq Ste I, Abbeville, AL 36310-2135",
     {"city": "Abbeville", "state": "AL", "postal_code": "36310"}),
    ("36 Tanner St., Ste. 300, Haddonfield, NJ 08033",
     {"city": "Haddonfield", "state": "NJ", "postal_code": "08033"}),
    # city, ST, zip  (no street)
    ("Abbeville, AL 36310-0608",
     {"city": "Abbeville", "state": "AL", "postal_code": "36310"}),
    ("New York, NY 10016",
     {"city": "New York", "state": "NY", "postal_code": "10016"}),
    # P.O. box as the "street"
    ("P.O. Box 610, Abbeville, AL 36310",
     {"city": "Abbeville", "state": "AL", "postal_code": "36310"}),
    # multi-word cities usaddress truncates ("Quinta" / "Peninsula")
    ("La Quinta, CA 92248-5969",
     {"city": "La Quinta", "state": "CA", "postal_code": "92248"}),
    ("Palos Verdes Peninsula, CA 90274-9570",
     {"city": "Palos Verdes Peninsula", "state": "CA", "postal_code": "90274"}),
    ("Half Moon Bay, CA 94019",
     {"city": "Half Moon Bay", "state": "CA", "postal_code": "94019"}),
    # hyphenated city
    ("Cardiff-By-The-Sea, CA 92007",
     {"city": "Cardiff-By-The-Sea", "state": "CA", "postal_code": "92007"}),
    ("Winston-Salem, NC 27101",
     {"city": "Winston-Salem", "state": "NC", "postal_code": "27101"}),
    # malformed 9-digit (no hyphen) ZIP -> take first 5
    ("2501 South Broadway, Little Rock, AR 722060000",
     {"city": "Little Rock", "state": "AR", "postal_code": "72206"}),
    # suite + tower
    ("1 World Trade Center, Suite 8500, New York, NY 10007",
     {"city": "New York", "state": "NY", "postal_code": "10007"}),
    # no ZIP at all
    ("Tuscaloosa, AL",
     {"city": "Tuscaloosa", "state": "AL", "postal_code": None}),
    # Alex's illustrative shape: city + state, comma form
    ("New York, New York",
     {"city": "New York", "state": None, "postal_code": None}),  # "New York" != USPS code -> no state anchor
    # genuinely foreign: must NOT fabricate a US state (4-digit "7935" is not a US ZIP)
    ("Cape Town, South Africa 7935",
     {"city": "South Africa 7935", "state": None, "postal_code": None}),
    # empty / junk
    ("", {"city": None, "state": None, "postal_code": None}),
]


def _normalized_from_parse(city_raw: str) -> dict[str, Any] | None:
    """Build an ``offices[].normalized`` dict from a crammed location string,
    or None if nothing parseable."""
    na = parse_full_location(city_raw)
    if na is None:
        return None
    norm = {
        "street": na.street_normalized,
        "city": na.city,
        "state": na.state,
        "postal_code": na.postal_code,
        "country": na.country or "US",
    }
    if not any(v and v != "US" for v in norm.values()):
        return None
    return norm


def repair_office(office: dict[str, Any]) -> bool:
    """If this office's normalized.state is missing but city_raw holds a full
    address, rebuild normalized from it. Returns True if it changed. Offices
    that already have a normalized.state are left untouched."""
    norm = office.get("normalized") or {}
    if norm.get("state"):
        return False
    city_raw = (office.get("city_raw") or "").strip()
    if not city_raw:
        return False
    new = _normalized_from_parse(city_raw)
    if new is None or new == norm:
        return False
    office["normalized"] = new
    return True


# ---------------------------------------------------------------------------


def run_backstop() -> int:
    print("=== BACK-STOP: curated input -> expected ===")
    failures = 0
    for raw, exp in BACKSTOP_CASES:
        na = parse_full_location(raw)
        got = {
            "city": na.city if na else None,
            "state": na.state if na else None,
            "postal_code": na.postal_code if na else None,
        }
        ok = all(got[k] == exp[k] for k in ("city", "state", "postal_code"))
        if not ok:
            failures += 1
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] {raw!r}")
        if not ok:
            print(f"          expected {exp}")
            print(f"          got      {got}")
    print(f"\n{len(BACKSTOP_CASES) - failures}/{len(BACKSTOP_CASES)} cases pass.")
    return 1 if failures else 0


def _candidate_ids(session: Session) -> list[int]:
    # Martindale rows whose primary office has no normalized.state.
    rows = session.execute(
        text(
            "SELECT id FROM firm_source_records "
            "WHERE source='martindale' "
            "AND json_extract(offices,'$[0].normalized.state') IS NULL "
            "AND offices IS NOT NULL AND offices != '[]'"
        )
    )
    return [r[0] for r in rows]


def run_dry_run(sample: int) -> int:
    engine = make_engine()
    with Session(engine) as s:
        ids = _candidate_ids(s)
    print(f"=== DRY-RUN (no writes) === candidate martindale rows: {len(ids)}")
    fixed = state_recovered = unparseable = 0
    diffs: list[tuple[int, str, dict[str, Any]]] = []
    chunk = 5000
    for start in range(0, len(ids), chunk):
        with Session(engine) as s:
            for r in s.scalars(
                select(FirmSourceRecord).where(
                    FirmSourceRecord.id.in_(ids[start : start + chunk])
                )
            ):
                offices = r.offices or []
                row_changed = False
                for o in offices:
                    before = (o.get("normalized") or {}).get("state")
                    if repair_office(o):
                        row_changed = True
                        after = (o.get("normalized") or {}).get("state")
                        if after and not before:
                            state_recovered += 1
                        if len(diffs) < sample:
                            diffs.append((r.id, o.get("city_raw", ""), o["normalized"]))
                if row_changed:
                    fixed += 1
                else:
                    unparseable += 1
    print(f"rows that would change      : {fixed}")
    print(f"primary-state recovered     : {state_recovered}")
    print(f"rows left unchanged         : {unparseable}  (foreign / unparseable)")
    print(f"\nsample of {len(diffs)} before/after:")
    for rid, cr, norm in diffs:
        print(f"  id={rid}  city_raw={cr!r}")
        print(f"        -> normalized={norm}")
    print("\n(no rows were written — pass --apply to persist)")
    return 0


def run_apply() -> int:
    engine = make_engine()
    with Session(engine) as s:
        ids = _candidate_ids(s)
    print(f"=== APPLY === candidate rows: {len(ids)}")
    written = 0
    chunk = 5000
    for start in range(0, len(ids), chunk):
        with Session(engine) as s:
            for r in s.scalars(
                select(FirmSourceRecord).where(
                    FirmSourceRecord.id.in_(ids[start : start + chunk])
                )
            ):
                offices = r.offices or []
                if any(repair_office(o) for o in list(offices)):
                    r.offices = offices
                    flag_modified(r, "offices")
                    written += 1
            s.commit()
    print(f"Wrote repaired offices on {written} rows.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--backstop", action="store_true", help="run curated cases")
    g.add_argument("--apply", action="store_true", help="persist changes (chunked)")
    ap.add_argument("--sample", type=int, default=20, help="dry-run sample size")
    args = ap.parse_args()
    if args.backstop:
        return run_backstop()
    if args.apply:
        return run_apply()
    return run_dry_run(args.sample)


if __name__ == "__main__":
    sys.exit(main())
