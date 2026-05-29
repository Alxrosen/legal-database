"""Inspect the Martindale FirmSourceRecord rows after a pilot run."""

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
from legal_sourcing.normalize.title_rank import classify_title  # noqa: E402


def main() -> int:
    e = create_engine(get_settings().db_url)
    with Session(e) as s:
        rows = s.scalars(
            select(FirmSourceRecord)
            .where(FirmSourceRecord.source == "martindale")
            .order_by(FirmSourceRecord.attorney_count.desc().nullslast(), FirmSourceRecord.name_raw)
        ).all()

    print(f"Total martindale firms : {len(rows)}")
    print(f"  multi-attorney      : {sum(1 for r in rows if (r.attorney_count or 0) > 1)}")
    print(f"  with website        : {sum(1 for r in rows if r.website_raw)}")
    print(f"  with phone (firm)   : {sum(1 for r in rows if r.phone_raw)}")
    by_state = Counter(((r.additional_data or {}).get("state_slug") or "?") for r in rows)
    print(f"  by state            : {dict(by_state)}")

    multi = [r for r in rows if (r.attorney_count or 0) > 1]
    print()
    print(f"Top multi-attorney firms ({len(multi)} total):")
    for r in sorted(multi, key=lambda r: -r.attorney_count)[:8]:
        site = (r.website_raw or "—")[:40]
        print(f"  attys={r.attorney_count}  {r.name_raw}  [site: {site}]")
        for c in r.contacts:
            t = c.get("title") or "(none)"
            rank = classify_title(t)
            nm = c.get("name_raw") or ""
            print(f"      - {nm:25s}  title={t!r:30s}  rank={rank}")

    print()
    print("Title coverage:")
    titles: Counter = Counter()
    ranks_known = 0
    ranks_unknown = 0
    for r in rows:
        for c in r.contacts:
            t = c.get("title")
            if t:
                titles[t] += 1
                if classify_title(t) is not None:
                    ranks_known += 1
                else:
                    ranks_unknown += 1
    print(f"  attorneys with title : {sum(titles.values())} (classified={ranks_known}, unclassified={ranks_unknown})")
    print(f"  distinct titles      : {len(titles)}")
    print(f"  top 12:")
    for t, n in titles.most_common(12):
        print(f"    {t!r:35s}  n={n}  rank={classify_title(t)}")

    print()
    print("Sample firm row (full):")
    if rows:
        r = sorted(multi, key=lambda r: -r.attorney_count)[0] if multi else rows[0]
        print(f"  name_raw           : {r.name_raw!r}")
        print(f"  name_normalized    : {r.name_normalized!r}")
        print(f"  website_raw        : {r.website_raw!r}")
        print(f"  website_normalized : {r.website_normalized!r}")
        print(f"  phone_raw          : {r.phone_raw!r}")
        print(f"  phone_normalized   : {r.phone_normalized!r}")
        print(f"  attorney_count     : {r.attorney_count}")
        print(f"  deactivation_status: {r.deactivation_status!r}")
        print(f"  source_url         : {r.source_url}")
        print(f"  additional_data    : {r.additional_data}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
