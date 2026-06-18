"""Golden extractor regression suite (Surveyor captures, Websites fixes).

Each row in ``data/eval/extraction_golden.csv`` pins one extracted field to its
ground-truth value, backed by the committed raw HTML under
``tests/fixtures/qa_cases/<case_id>/``. The test runs the REAL extractor
(``extract_site``) over the saved pages and asserts the value — entirely offline,
so nobody re-fetches and a fixed bug can never silently revert.

A newly-captured bad case lands RED here: that red test IS the work order handed
to Websites. Websites iterates ``website_extract.py`` against the saved HTML until
it goes GREEN; the fixture then stays in the suite forever.

``now_year`` is pinned per row (years are relative to "now") so assertions don't
rot over calendar time.
"""

from __future__ import annotations

import pytest

from legal_sourcing.enrichment.website_extract import extract_site
from legal_sourcing.resolution.qa_sample import (
    FIXTURES_DIR,
    GOLDEN_CSV,
    GOLDEN_FIELDS,
    GOLDEN_HEADER,
    read_fixture_pages,
)


def _golden_rows() -> list[dict[str, str]]:
    import csv

    if not GOLDEN_CSV.exists():
        return []
    with GOLDEN_CSV.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _load_pages(case_id: str) -> list[tuple[str, bytes]]:
    return read_fixture_pages(FIXTURES_DIR / case_id)


_ROWS = _golden_rows()
_PARAMS = _ROWS or [
    pytest.param({}, marks=pytest.mark.skip(reason="no golden cases captured yet"), id="empty")
]


def test_golden_csv_is_wellformed() -> None:
    """The CSV header is stable and every row points at a real fixture + a known
    field. (Always runs, even before any case is captured.)"""
    import csv

    if not GOLDEN_CSV.exists():
        pytest.skip("golden CSV not present")
    with GOLDEN_CSV.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == GOLDEN_HEADER, reader.fieldnames
        for row in reader:
            assert row["field"] in GOLDEN_FIELDS, f"unknown field {row['field']!r}"
            case_dir = FIXTURES_DIR / row["case_id"]
            assert (case_dir / "home.html.gz").exists() or (case_dir / "home.html").exists(), (
                f"missing fixture home page for {row['case_id']}"
            )
            int(row["now_year"])  # parses


@pytest.mark.parametrize(
    "row",
    _PARAMS,
    ids=[f"{r['case_id']}:{r['field']}" for r in _ROWS] if _ROWS else None,
)
def test_extraction_matches_golden(row: dict[str, str]) -> None:
    pages = _load_pages(row["case_id"])
    assert pages, f"no fixture HTML for case {row['case_id']!r}"
    accessor, parse_expected, equal = GOLDEN_FIELDS[row["field"]]
    site = extract_site(pages, now_year=int(row["now_year"]))
    actual = accessor(site)
    expected = parse_expected(row["expected_value"])
    assert equal(actual, expected), (
        f"[{row['case_id']}] {row['field']}: extracted {actual!r}, expected {expected!r} "
        f"({row.get('note') or 'no note'})"
    )
