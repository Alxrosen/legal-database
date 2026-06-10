"""Recover firm names on FindLaw attorney-card rows — RENAME IN PLACE, never delete.

Root cause (Fixer's lane): FindLaw SRPs interleave firm cards and ATTORNEY cards
under the same ``.fl-serp-card.organic`` class. The old ``_extract_card`` took the
card title as ``name_raw`` — so ~4.7k attorney cards stored the PERSON as a firm
("Scott Cohen" instead of "The Schiller Kessler Group"). The firm name is present
on every cached card as the ``a[data-testid="fl-serp-card-parent-link"]`` anchor
(100% of attorney cards in the cache carry it), so the fix is recovery from cache —
NO re-scrape, NO deletion (per Alex: these are real firms with corrupted names).

Why in-place (not a pipeline re-load): ``source_firm_id`` is a content hash of
``(name_normalized, street_normalized)``, so re-loading with the fixed parser would
emit rows under NEW firm-keyed ids and leave the 4.7k legacy person-keyed rows
stale. Instead each legacy row keeps its id/key and gets: ``name_raw``/
``name_normalized`` = the parent firm, the person moved into ``contacts``, and an
audit trail in ``additional_data.attorney_card_recovery``. Resolution then merges
these rows with the firm's own organic-card / website / martindale rows naturally.

Modes:
  --backstop   Verify the two Alex-flagged rows' recovery from the cache index.
  (default)    DRY-RUN: build the cache index, report match/rename counts +
               samples. Writes nothing.
  --apply      Rename in place, 1k-row chunks via make_engine(). Idempotent
               (skips rows already carrying ``attorney_card_recovery``).
"""

from __future__ import annotations

import argparse
import gzip
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from selectolax.parser import HTMLParser  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from legal_sourcing.config import get_settings  # noqa: E402
from legal_sourcing.db import make_engine  # noqa: E402
from legal_sourcing.models import FirmSourceRecord  # noqa: E402
from legal_sourcing.normalize.name import normalize_firm_name  # noqa: E402
from legal_sourcing.parsers.findlaw import _profile_id  # noqa: E402

# The two rows Alex flagged. They are TWO DIFFERENT attorneys who happen to share
# a name (different slug-ids, different states) — which is exactly why storing the
# person as the firm was so dangerous: two unrelated firms collapsed under one
# person-name. Each must recover to its OWN parent firm.
ALEX_FLAGGED_EXPECT: dict[int, str] = {
    130789: "The Schiller Kessler Group",  # Scott Cohen, Davie FL (NTM4NzYyOF8x)
    136718: "Cohen Injury Law, P.C.",  # Scott Cohen, Marietta GA (NTM4NDM5Ml8x)
}


def build_cache_index() -> dict[str, dict[str, Any]]:
    """Scan every cached FindLaw SRP and map attorney slug-id ->
    {firm_name, firm_id, firm_url} from the parent-link anchor.

    Conflicts (same attorney, different parent across pages) resolve by
    majority; ties by first-seen.
    """
    base = get_settings().raw_data_dir / "findlaw"
    votes: dict[str, Counter] = {}
    meta: dict[tuple[str, str], dict[str, Any]] = {}
    pages = sorted(base.glob("*/city/*/*/*.html.gz"))
    for gz in pages:
        try:
            html = gzip.decompress(gz.read_bytes()).decode("utf-8", "replace")
        except Exception:
            continue
        tree = HTMLParser(html)
        for card in tree.css(".fl-serp-card.organic"):
            cls = (card.attributes.get("class") or "").split()
            if "attorney" not in cls:
                continue
            title = card.css_first('a[data-testid="serp-card-title-link"], a.fl-serp-card-title')
            if title is None:
                continue
            attorney_id = _profile_id(title.attributes.get("href"))
            if not attorney_id:
                continue
            parent = card.css_first('a[data-testid="fl-serp-card-parent-link"]')
            if parent is None:
                continue
            firm_name = parent.text(strip=True) or None
            if not firm_name:
                continue
            firm_url = parent.attributes.get("href")
            votes.setdefault(attorney_id, Counter())[firm_name] += 1
            meta.setdefault(
                (attorney_id, firm_name),
                {
                    "firm_name": firm_name,
                    "firm_id": _profile_id(firm_url),
                    "firm_url": firm_url,
                },
            )
    index: dict[str, dict[str, Any]] = {}
    for attorney_id, counter in votes.items():
        winner = counter.most_common(1)[0][0]
        index[attorney_id] = meta[(attorney_id, winner)]
    return index


def _candidate_rows(session: Session) -> list[int]:
    rows = session.execute(
        text(
            "SELECT id FROM firm_source_records WHERE source='findlaw' "
            "AND json_extract(additional_data,'$.data_testid') LIKE 'attorney-card%'"
        )
    )
    return [r[0] for r in rows]


def _plan_row(r: FirmSourceRecord, index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Compute the rename for one legacy row, or None if no action.

    The legacy ``additional_data.source_firm_id_findlaw`` held the ATTORNEY's
    slug id (the old parser read it off the title link).
    """
    ad = dict(r.additional_data or {})
    if "attorney_card_recovery" in ad:
        return None  # already recovered (idempotency)
    attorney_id = ad.get("source_firm_id_findlaw")
    hit = index.get(attorney_id) if attorney_id else None
    if hit is None:
        return None
    return {"attorney_id": attorney_id, **hit}


def _apply_row(r: FirmSourceRecord, plan: dict[str, Any]) -> None:
    person_name = r.name_raw
    firm_name = plan["firm_name"]
    name_n = normalize_firm_name(firm_name)
    person_n = normalize_firm_name(person_name)

    ad = dict(r.additional_data or {})
    ad["card_type"] = "attorney"
    ad["source_attorney_id_findlaw"] = plan["attorney_id"]
    ad["attorney_profile_url"] = ad.get("firm_profile_url")  # legacy value = attorney URL
    if plan.get("firm_id"):
        ad["source_firm_id_findlaw"] = plan["firm_id"]
    if plan.get("firm_url"):
        ad["firm_profile_url"] = plan["firm_url"]
    ad["attorney_card_recovery"] = {
        "original_name_raw": person_name,
        "recovered_via": "fl-serp-card-parent-link (cached SRP)",
    }

    contacts = list(r.contacts or [])
    if not any((c.get("name_raw") or "") == (person_name or "") for c in contacts):
        contacts.append(
            {
                "name_raw": person_name,
                "name_normalized": person_n.normalized if person_n else None,
                "title": None,
                "phone_raw": r.phone_raw,
                "phone_normalized": r.phone_normalized,
                "source_attorney_id": plan["attorney_id"],
                "source_attorney_url": ad.get("attorney_profile_url"),
            }
        )

    r.name_raw = firm_name
    r.name_normalized = name_n.normalized if name_n else None
    r.contacts = contacts
    r.attorney_count = len(contacts)
    r.additional_data = ad
    flag_modified(r, "contacts")
    flag_modified(r, "additional_data")


def run(apply: bool, sample: int) -> int:
    print("Building cache index (attorney id -> parent firm)...")
    index = build_cache_index()
    print(f"  index covers {len(index)} attorneys")

    engine = make_engine()
    with Session(engine) as s:
        ids = _candidate_rows(s)
    print(f"legacy attorney-card rows in DB: {len(ids)}")

    renamed = unmatched = already = 0
    samples: list[tuple[int, str | None, str]] = []
    chunk = 1000
    for start in range(0, len(ids), chunk):
        with Session(engine) as s:
            for r in s.scalars(
                select(FirmSourceRecord).where(FirmSourceRecord.id.in_(ids[start : start + chunk]))
            ):
                ad = r.additional_data or {}
                if "attorney_card_recovery" in ad:
                    already += 1
                    continue
                plan = _plan_row(r, index)
                if plan is None:
                    unmatched += 1
                    continue
                if len(samples) < sample:
                    samples.append((r.id, r.name_raw, plan["firm_name"]))
                if apply:
                    _apply_row(r, plan)
                renamed += 1
            if apply:
                s.commit()

    mode = "APPLIED" if apply else "DRY-RUN (no writes)"
    print(f"\n=== {mode} ===")
    print(f"  renamed person->firm : {renamed}")
    print(f"  already recovered    : {already}")
    print(f"  no cache match (kept as-is): {unmatched}")
    print("\nsample (id, person -> firm):")
    for rid, person, firm in samples:
        print(f"  {rid}  {person!r} -> {firm!r}")
    return 0


def run_backstop() -> int:
    index = build_cache_index()
    engine = make_engine()
    failures = 0
    with Session(engine) as s:
        for rid, expected in ALEX_FLAGGED_EXPECT.items():
            r = s.get(FirmSourceRecord, rid)
            if r is None:
                print(f"  [FAIL] id={rid} not found")
                failures += 1
                continue
            ad = r.additional_data or {}
            if "attorney_card_recovery" in ad:
                ok = r.name_raw == expected
                print(f"  [{'PASS' if ok else 'FAIL'}] id={rid} already recovered: {r.name_raw!r}")
                failures += 0 if ok else 1
                continue
            plan = _plan_row(r, index)
            got = plan["firm_name"] if plan else None
            ok = got == expected
            print(f"  [{'PASS' if ok else 'FAIL'}] id={rid} {r.name_raw!r} -> {got!r} (want {expected!r})")
            failures += 0 if ok else 1
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--backstop", action="store_true", help="verify the Alex-flagged rows")
    g.add_argument("--apply", action="store_true", help="persist the renames (chunked)")
    ap.add_argument("--sample", type=int, default=15)
    args = ap.parse_args()
    if args.backstop:
        return run_backstop()
    return run(apply=args.apply, sample=args.sample)


if __name__ == "__main__":
    sys.exit(main())
