"""AZ Bar reconnaissance — capture fixtures + confirm endpoint shapes.

This is the manual-but-scripted step the reference doc calls for
BEFORE writing parser / pipeline code. It exercises every endpoint
listed in docs/data_sources/az_bar_reference.md and saves:

  * raw gzipped responses under data/raw/az_bar/{date}/recon/{phase}/...
    (the production storage layout, useful for end-to-end parser tests
    later)
  * pretty-printed copies under tests/fixtures/az_bar/recon/...
    (committed, so future work can be done against deterministic data)

It also prints a per-endpoint summary:
  * status, response time, body size
  * envelope shape (IsSuccess / Result.TotalCount / array length)
  * top-level keys on a sample record (for unconfirmed endpoints)

Failures:
  * 401/403 -> AZBarApiPasswordRotatedError with re-capture instructions.
  * Any other non-2xx surfaces with the URL, status, and a short body
    excerpt.

Usage:
    uv run python scripts/recon_azbar.py

Optional:
    --entity-number 35371   # use a specific attorney for detail
    --skip-edges            # skip pagination-edge probes (faster)
    --pi-only               # only run the PI-specialization probe
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

# Make `legal_sourcing` importable when run directly.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from legal_sourcing.scrapers.az_bar import (  # noqa: E402
    AZBarApiPasswordRotatedError,
    AZBarScraper,
)
from legal_sourcing.scrapers.base import ScrapeError  # noqa: E402
from legal_sourcing.utils.logging import configure_logging, get_logger  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures" / "az_bar" / "recon"
log = get_logger(__name__)


def _save_fixture(name: str, data: Any) -> Path:
    """Pretty-print a fixture for human review and parser tests."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURES_DIR / f"{name}.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def _read_gz_json(path: Path) -> Any:
    with gzip.open(path, "rb") as f:
        return json.loads(f.read())


def _summarize(name: str, data: Any) -> str:
    """Compact one-line shape summary for a response payload."""
    if isinstance(data, list):
        sample_keys = sorted(data[0].keys()) if data and isinstance(data[0], dict) else []
        return f"{name}: bare array, n={len(data)}, keys={sample_keys}"
    if isinstance(data, dict):
        if "IsSuccess" in data:  # standard envelope
            r = data.get("Result", {}) or {}
            if isinstance(r, dict) and "Results" in r:
                inner = r.get("Results", [])
                inner_keys = sorted(inner[0].keys()) if inner and isinstance(inner[0], dict) else []
                return (
                    f"{name}: envelope, IsSuccess={data.get('IsSuccess')}, "
                    f"TotalCount={r.get('TotalCount')}, len(Results)={len(inner)}, "
                    f"record_keys={inner_keys}"
                )
            return (
                f"{name}: envelope, IsSuccess={data.get('IsSuccess')}, "
                f"Result keys={sorted(r.keys()) if isinstance(r, dict) else type(r).__name__}"
            )
        return f"{name}: bare object, keys={sorted(data.keys())}"
    return f"{name}: {type(data).__name__}"


# ---- Phase runners ----------------------------------------------------


def run_reference(scraper: AZBarScraper) -> dict[str, Any]:
    """Fetch every small reference dropdown + Specializations + ABS."""
    payloads: dict[str, Any] = {}
    endpoints = [
        ("specializations", scraper.SPECIALIZATIONS_PATH, False),
        ("states", scraper.STATES_PATH, False),
        ("counties", scraper.COUNTIES_PATH, False),
        ("jurisdictions", scraper.JURISDICTIONS_PATH, False),
        ("languages", scraper.LANGUAGES_PATH, False),
        ("law_schools", scraper.LAW_SCHOOLS_PATH, False),
        ("sections", scraper.SECTIONS_PATH, False),
        ("abs", scraper.ABS_PATH, True),  # IncludeInactive=true
    ]
    for name, path, include_inactive in endpoints:
        url = scraper.reference_url(path, include_inactive=include_inactive)
        t0 = time.monotonic()
        gz = scraper.fetch_one(url, method="GET", bucket="recon/reference", filename=name)
        elapsed = time.monotonic() - t0
        data = _read_gz_json(gz)
        payloads[name] = data
        _save_fixture(f"reference_{name}", data)
        print(f"  {_summarize(name, data)}  ({elapsed*1000:.0f}ms, {gz.stat().st_size} B gz)")
    return payloads


def run_list_page1(scraper: AZBarScraper) -> dict[str, Any]:
    url = scraper.list_url(page=1, page_size=25, shuffle=False)
    t0 = time.monotonic()
    gz = scraper.fetch_one(url, method="POST", json_body={}, bucket="recon/list", filename="page_0001")
    elapsed = time.monotonic() - t0
    data = _read_gz_json(gz)
    _save_fixture("list_page_0001", data)
    print(f"  {_summarize('list page 1', data)}  ({elapsed*1000:.0f}ms)")
    return data


def run_detail(scraper: AZBarScraper, entity_number: int) -> dict[str, Any]:
    url = scraper.detail_url(entity_number)
    t0 = time.monotonic()
    gz = scraper.fetch_one(
        url, method="GET", bucket="recon/detail", filename=str(entity_number)
    )
    elapsed = time.monotonic() - t0
    data = _read_gz_json(gz)
    _save_fixture(f"detail_{entity_number}", data)
    print(f"  {_summarize(f'detail {entity_number}', data)}  ({elapsed*1000:.0f}ms)")
    return data


def run_pagination_edges(scraper: AZBarScraper, total_count: int) -> None:
    last_page = max(1, (total_count + 24) // 25)
    probes = [
        ("page_last", last_page),
        ("page_past_end", last_page + 50),
    ]
    for name, page in probes:
        url = scraper.list_url(page=page, page_size=25, shuffle=False)
        try:
            gz = scraper.fetch_one(
                url, method="POST", json_body={}, bucket="recon/list", filename=name
            )
            data = _read_gz_json(gz)
            _save_fixture(f"list_{name}", data)
            print(f"  {_summarize(f'list {name} (Page={page})', data)}")
        except ScrapeError as exc:
            print(f"  list {name} (Page={page}) -> ScrapeError: {exc}")


def run_page_size_probes(scraper: AZBarScraper) -> None:
    for size in (100, 200):
        url = scraper.list_url(page=1, page_size=size, shuffle=False)
        try:
            gz = scraper.fetch_one(
                url,
                method="POST",
                json_body={},
                bucket="recon/list",
                filename=f"page_0001_pagesize_{size}",
            )
            data = _read_gz_json(gz)
            _save_fixture(f"list_pagesize_{size}", data)
            print(f"  {_summarize(f'PageSize={size}', data)}")
        except ScrapeError as exc:
            print(f"  PageSize={size} -> ScrapeError: {exc}")


def run_pi_specialization(scraper: AZBarScraper) -> None:
    url = scraper.list_url(page=1, page_size=25, shuffle=False, specialization_code="PI")
    try:
        gz = scraper.fetch_one(
            url, method="POST", json_body={}, bucket="recon/list", filename="page_0001_pi"
        )
        data = _read_gz_json(gz)
        _save_fixture("list_pi_specialization", data)
        print(f"  {_summarize('PI specialization filter', data)}")
    except ScrapeError as exc:
        print(f"  PI specialization probe -> ScrapeError: {exc}")


# ---- Main -------------------------------------------------------------


def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity-number", type=int, default=None)
    parser.add_argument("--skip-edges", action="store_true")
    parser.add_argument("--pi-only", action="store_true")
    args = parser.parse_args()

    print("== AZ Bar reconnaissance ==\n")

    try:
        scraper = AZBarScraper()
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    try:
        if args.pi_only:
            print("[PI-only] PI specialization filter")
            run_pi_specialization(scraper)
            return 0

        print("[1/5] Reference dropdowns + Specializations + ABS")
        run_reference(scraper)

        print("\n[2/5] List page 1 (PageSize=25, Shuffle=false)")
        list_page1 = run_list_page1(scraper)
        total_count = (list_page1.get("Result") or {}).get("TotalCount")
        if total_count:
            print(f"        TotalCount = {total_count}")

        # Pick an EntityNumber for the detail probe.
        entity_number = args.entity_number
        if entity_number is None:
            results = (list_page1.get("Result") or {}).get("Results") or []
            if not results:
                print("        ! No results in list page 1; cannot pick an EntityNumber.")
            else:
                entity_number = results[0]["EntityNumber"]

        if entity_number is not None:
            print(f"\n[3/5] Detail endpoint for EntityNumber={entity_number}")
            run_detail(scraper, entity_number)

        if not args.skip_edges and total_count:
            print("\n[4/5] Pagination edges")
            run_pagination_edges(scraper, total_count)

            print("\n[5/5] PageSize probes (100, 200) + PI specialization filter")
            run_page_size_probes(scraper)
            run_pi_specialization(scraper)

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            raise AZBarApiPasswordRotatedError(
                "AZ Bar returned 401/403 — the Password header has likely "
                "rotated. Re-capture per docs/data_sources/az_bar_reference.md "
                "and update AZBAR_API_PASSWORD in .env."
            ) from exc
        raise
    except ScrapeError as exc:
        # Our base scraper wraps 401/403 into ScrapeError("... non-retryable").
        msg = str(exc)
        if " 401 " in msg or " 403 " in msg or "-> 401" in msg or "-> 403" in msg:
            print(
                "\nFATAL: 401/403 from AZ Bar. The Password header has likely "
                "rotated. Re-capture per "
                "docs/data_sources/az_bar_reference.md and update "
                "AZBAR_API_PASSWORD in .env.",
                file=sys.stderr,
            )
            return 3
        raise
    finally:
        scraper.close()

    print("\nDone. Fixtures saved under tests/fixtures/az_bar/recon/.")
    print("Next: update docs/data_sources/az_bar_reference.md with confirmed shapes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
