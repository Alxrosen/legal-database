"""Re-run the Martindale recon extractors against the saved gz pages.

This proves the "re-parsing never requires re-scraping" principle: edit
the extractors, re-run, update the fixtures, no extra Martindale hits.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from recon_martindale import (  # noqa: E402
    extract_attorney_cards,
    extract_cities,
    extract_firm_profile,
    extract_states,
)

FIXTURES_DIR = ROOT / "tests" / "fixtures" / "martindale" / "recon"
RAW_ROOT = ROOT / "data" / "raw" / "martindale"


def _read_gz(path: Path) -> str:
    with gzip.open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def _save(name: str, data) -> Path:
    out = FIXTURES_DIR / f"{name}.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def main() -> int:
    # Find the most recent date folder.
    date_dirs = sorted([p for p in RAW_ROOT.iterdir() if p.is_dir()])
    if not date_dirs:
        print("no Martindale raw data found", file=sys.stderr)
        return 1
    day = date_dirs[-1]
    print(f"Re-parsing from: {day}")

    pairs = [
        ("state_index", day / "index" / "find_attorneys.html.gz", extract_states),
        ("alabama_cities", day / "state" / "alabama.html.gz", extract_cities),
        (
            "abbeville_alabama_attorneys",
            day / "city" / "abbeville_alabama_p1.html.gz",
            extract_attorney_cards,
        ),
        ("sample_firm_profile", day / "firm" / "sample_firm.html.gz", extract_firm_profile),
    ]
    for name, path, extractor in pairs:
        if not path.exists():
            print(f"  skip {name}: {path} missing")
            continue
        html = _read_gz(path)
        data = extractor(html)
        out = _save(name, data)
        if isinstance(data, list):
            print(f"  {name}: {len(data)} items -> {out}")
        else:
            print(f"  {name}: {data} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
