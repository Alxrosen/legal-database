"""Cross-source generic/junk firm-name guard (martindale / az_bar / findlaw).

Generic names ("Attorney at Law", "Alabama Personal Injury Law Firm", "Visa Inc")
are false-merge magnets: Canonizer's name+city+state floor would fuse two unrelated
"Phoenix Law Firm" rows in the same city. Per the shared-util policy
(``normalize/firm_name.py``: rename-not-drop; NULL only as last resort; KEEP the
row), the treatment here is deliberately minimal and non-destructive:

  * ``name_raw`` is NEVER touched — it is verbatim source evidence (martindale /
    az_bar names are what the source listed; there is no better in-source name).
  * ``name_normalized`` (the MERGE KEY) is cleared, so the junk name can't drive a
    false merge. Rows that carry a website / phone still resolve through those
    identity signals, and the real name then arrives in the cluster via the
    ``source="website"`` row at fusion time (cross-source recovery-by-merge).
  * ``additional_data.name_quality`` records the reason — auditable + reversible
    (re-deriving name_normalized from name_raw is one re-normalize away).

The flag decision is the shared util's ``low_quality_reason`` verbatim. The
governing criterion — **clear the merge key only when the name carries ZERO
distinctive content** — was tuned here against an adversarial review of ALL
flagged names (the original util flagged real digit/initials/URL-form brands at
a ~12-20% rate) and the resulting rescues were then folded INTO the util
(`b85c90e`): digit/initials tokens count as identity ("The 928 Law Firm",
"J.K. Lawyers", "The H Law Group"), "&" names are identity, URL-form names with
a distinctive stem are identity ("Otto.Law", "BrentCorwin.com"), and a firm's
own domain rescues via containment/prefix/acronym echo ("MAS Law"/mas.law,
"Best Law Firm"/bestlawaz.com, "Business Law Center"/blc-plc.com). A bare
entity suffix is deliberately NOT identity ("A Law Firm, P.C." stays flagged:
"a law firm" is exactly the merge key this script exists to disarm). The
BACKSTOP_CASES below are the validated contract (mirrored as util tests).

Modes:
  --backstop   Curated cases: junk flags, real-firm rescues.
  (default)    DRY-RUN: per-source counts + samples. Writes nothing.
  --apply      Clear name_normalized + tag, 1k-row chunks via make_engine().
               Idempotent (skips rows already tagged with the same reason).

NB (run order): the martindale/az_bar shared upsert overwrites name_normalized +
additional_data wholesale, so any future `scrape_martindale load`/`enrich` wipes
this treatment on re-touched rows — re-run this script after such loads (cheap,
idempotent).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from legal_sourcing.db import make_engine  # noqa: E402
from legal_sourcing.models import FirmSourceRecord  # noqa: E402
from legal_sourcing.normalize.firm_name import low_quality_reason  # noqa: E402

SOURCES = ("martindale", "az_bar", "findlaw")

# (name, host, expected reason or None)
BACKSTOP_CASES: list[tuple[str, str | None, str | None]] = [
    # unambiguous junk -> flagged
    ("Attorney at Law", None, "generic"),
    ("law office", None, "generic"),
    ("Law", None, "generic"),
    ("Alabama Personal Injury Law Firm", None, "descriptor"),
    ("Florida Injury Law Group", None, "descriptor"),
    # bare entity suffix is NOT identity -> still flagged
    ("A Law Firm, P.C.", None, "generic"),
    # descriptor on its OWN domain -> brand, kept (util host rescue)
    ("Nevada Family Law Group", "nevadafamilylaw.com", None),
    # ampersand names -> kept
    ("J&Y Law", "jnylaw.com", None),
    ("F&B Law Firm, P.C.", None, None),
    ("M&H Legal Services, LLC", None, None),
    ("Treon & Shook, PLLC", None, None),
    ("Morgan & Morgan", None, None),
    # digit brands -> kept (adversarial-review false positives)
    ("The 928 Law Firm", "928law.com", None),
    ("D2 Injury Law", "denmonpearlman.com", None),
    ("5280 Law Group", None, None),
    ("805 Law Group", None, None),
    ("615 Lawyer", None, None),
    ("THE702FIRM Injury Attorneys", "the702firm.com", None),
    # initials brands (incl. dotted) -> kept
    ("The H Law Group", "thehfirm.com", None),
    ("The W Law Firm", None, None),
    ("J.K. Lawyers", "jklawyers.com", None),
    ("S.M.F. Law", None, None),
    ("M.C. Law Group", None, None),
    ("A.R.K., Inc.", None, None),
    ("V.E.M. Attorney at Law", None, None),
    # URL-form names with a distinctive stem -> kept
    ("Otto.Law", "otto.law", None),
    ("BrentCorwin.com", None, None),
    ("Chewy.com", None, None),
    ("iTicket.Law - Powered by Hatley Law Office", "iticket.law", None),
    # own-domain echo: prefix / containment / acronym -> kept
    ("Best Law Firm", "bestlawaz.com", None),
    ("MAS Law", "mas.law", None),
    ("Business Law Center", "blc-plc.com", None),
    ("The Estate Planning Law Group", "teplg.com", None),
    ("Trusted Estate Planning Attorneys", "trustedepa.com", None),
    # ...but the SAME names with no domain to anchor them stay flagged
    ("Best Law Firm", None, "generic"),
    ("Business Law Center", None, "descriptor"),
]


def _flag(name: str, host: str | None) -> str | None:
    """The treatment decision for one stored name. None = leave untouched.

    This is the shared util verbatim — the distinctive-content rescues this
    script originally carried caller-side (digit/initials identity, host-echo,
    URL-stem, "&" structure) were folded into ``low_quality_reason`` itself
    (`b85c90e`); BACKSTOP_CASES pins that contract from the consumer side.
    """
    return low_quality_reason(name, host=host)


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        netloc = urlparse(url if "://" in url else f"http://{url}").netloc or url
    except ValueError:
        return None
    host = netloc.lower().split("@")[-1].split(":")[0]
    return host.removeprefix("www.") or None


def run_backstop() -> int:
    failures = 0
    for name, host, expected in BACKSTOP_CASES:
        got = _flag(name, host)
        ok = got == expected
        failures += 0 if ok else 1
        print(
            f"  [{'PASS' if ok else 'FAIL'}] {name!r} (host={host}) -> {got!r} (want {expected!r})"
        )
    print(f"\n{len(BACKSTOP_CASES) - failures}/{len(BACKSTOP_CASES)} cases pass.")
    return 1 if failures else 0


def _candidates(session: Session, source: str) -> list[int]:
    rows = session.execute(
        text(
            "SELECT id FROM firm_source_records WHERE source=:src "
            "AND name_raw IS NOT NULL AND name_raw != ''"
        ),
        {"src": source},
    )
    return [r[0] for r in rows]


def run(apply: bool, sample: int) -> int:
    engine = make_engine()
    grand = {"flagged": 0, "cleared": 0, "already": 0}
    for source in SOURCES:
        with Session(engine) as s:
            ids = _candidates(s, source)
        counts = {"named": len(ids), "generic": 0, "descriptor": 0, "already": 0}
        samples: list[tuple[int, str, str]] = []
        chunk = 1000
        for start in range(0, len(ids), chunk):
            with Session(engine) as s:
                for r in s.scalars(
                    select(FirmSourceRecord).where(
                        FirmSourceRecord.id.in_(ids[start : start + chunk])
                    )
                ):
                    host = _host_of(r.website_normalized or r.website_raw)
                    reason = _flag(r.name_raw or "", host)
                    if reason is None:
                        continue
                    ad = dict(r.additional_data or {})
                    tagged = (ad.get("name_quality") or {}).get("reason")
                    if tagged == reason and r.name_normalized is None:
                        counts["already"] += 1
                        grand["already"] += 1
                        continue
                    counts[reason] = counts.get(reason, 0) + 1
                    grand["flagged"] += 1
                    if len(samples) < sample:
                        samples.append((r.id, r.name_raw, reason))
                    if apply:
                        ad["name_quality"] = {
                            "reason": reason,
                            "name_normalized_cleared": True,
                            "via": "normalize/firm_name.py",
                        }
                        r.additional_data = ad
                        r.name_normalized = None
                        flag_modified(r, "additional_data")
                        grand["cleared"] += 1
                if apply:
                    s.commit()
        print(
            f"=== {source}: named={counts['named']}  flagged generic={counts['generic']} "
            f"descriptor={counts['descriptor']}  already-treated={counts['already']}"
        )
        for rid, name, reason in samples:
            print(f"     {rid}  {name!r}  [{reason}]")
    mode = "APPLIED" if apply else "DRY-RUN (no writes)"
    print(
        f"\n=== {mode} === flagged={grand['flagged']} cleared={grand['cleared']} "
        f"already={grand['already']}"
    )
    print("(name_raw untouched everywhere; treatment = clear name_normalized + tag)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--backstop", action="store_true")
    g.add_argument("--apply", action="store_true")
    ap.add_argument("--sample", type=int, default=10)
    args = ap.parse_args()
    if args.backstop:
        return run_backstop()
    return run(apply=args.apply, sample=args.sample)


if __name__ == "__main__":
    sys.exit(main())
