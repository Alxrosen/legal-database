"""DB-level guard for the QA loop: prove a captured bug, once fixed in the
extractor, cannot be silently re-introduced by ``enrich_websites load``.

The golden suite (``test_extraction_golden``) asserts ``extract_site`` in
isolation. This one drives the REAL ``run_load`` path end-to-end over the bucket
layout it reads from disk (gzip + sidecar), into a temp SQLite, and asserts the
stored ``website_enrichment`` values — so the load/upsert plumbing between the
extractor and the canonical DB is covered too.

* A synthetic inline case always runs (proves the load->DB->assert plumbing from
  day one, independent of any captured fixtures).
* Every committed golden case whose field has a stored ``website_enrichment``
  column is additionally driven through ``run_load`` and asserted at the DB level.
"""

from __future__ import annotations

import gzip
import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from legal_sourcing.models.base import Base
from legal_sourcing.pipelines import enrich_websites as ew
from legal_sourcing.pipelines.enrich_websites import _bucket
from legal_sourcing.resolution.qa_sample import FIXTURES_DIR, GOLDEN_CSV, read_fixture_pages

# Golden field -> the website_enrichment column it lands in (only the fields the
# crawl cache actually stores; e.g. year_founded / name_normalized live elsewhere).
_FIELD_TO_COLUMN = {
    "attorney_count": "attorney_count_min",
    "office_count": "office_count",
    "years_in_operation": "years_in_operation_min",
    "primary_city": "primary_city",
    "primary_state": "primary_state",
    "is_law_related": "is_law_related",
    "url_verification_status": "url_verification_status",
}


def _seed_raw(raw_dir, website: str, pages: dict[str, str | bytes]) -> None:
    """Write the gz bucket + home sidecar that run_load reads from disk."""
    bucket = _bucket(website)
    d = raw_dir / "firm_websites" / "2026-06-18" / bucket
    d.mkdir(parents=True, exist_ok=True)
    for role, html in pages.items():
        data = html if isinstance(html, bytes) else html.encode("utf-8")
        with gzip.open(d / f"{role}.html.gz", "wb") as f:
            f.write(data)
    (d / "home.json").write_text(
        json.dumps(
            {"url": f"https://{website}/", "final_url": f"https://{website}/", "status": 200}
        ),
        encoding="utf-8",
    )


def _run_load_into(tmp_path, monkeypatch, website: str, pages: dict[str, str]):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    _seed_raw(raw_dir, website, pages)
    eng = create_engine(f"sqlite:///{tmp_path / 'qa.sqlite'}")
    Base.metadata.create_all(eng)

    class _Settings:
        raw_data_dir = raw_dir

    monkeypatch.setattr(ew, "get_settings", lambda: _Settings())
    monkeypatch.setattr(ew, "make_engine", lambda: eng)
    ew.run_load()
    return eng


_SYNTHETIC_HTML = """
<html><head><title>Acme Law Firm, PLLC — Attorneys at Law</title></head>
<body>
  <h1>Acme Law Firm</h1>
  <p>Our attorneys provide legal representation in personal injury and family law.</p>
  <p>Contact our lawyers today. Practice areas: personal injury, family law.</p>
  <footer>123 Main St, Phoenix, AZ 85004 — (602) 555-0100</footer>
</body></html>
"""


def test_run_load_plumbing_synthetic(tmp_path, monkeypatch) -> None:
    """run_load reads the gz/sidecar bucket, re-extracts, and upserts a row."""
    eng = _run_load_into(tmp_path, monkeypatch, "acmelawfirm.com", {"home": _SYNTHETIC_HTML})
    with Session(eng) as s:
        row = (
            s.execute(
                text("SELECT website, is_law_related FROM website_enrichment WHERE website = :w"),
                {"w": "acmelawfirm.com"},
            )
            .mappings()
            .first()
        )
    assert row is not None, "run_load did not upsert the website_enrichment row"
    assert bool(row["is_law_related"]) is True


def _golden_db_cases() -> list[dict[str, str]]:
    import csv

    if not GOLDEN_CSV.exists():
        return []
    with GOLDEN_CSV.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r["field"] in _FIELD_TO_COLUMN]


_DB_CASES = _golden_db_cases()


@pytest.mark.skipif(not _DB_CASES, reason="no DB-backed golden cases captured yet")
@pytest.mark.parametrize(
    "row", _DB_CASES, ids=[f"{r['case_id']}:{r['field']}" for r in _DB_CASES] or None
)
def test_golden_case_survives_load(tmp_path, monkeypatch, row: dict[str, str]) -> None:
    case_dir = FIXTURES_DIR / row["case_id"]
    pages = {role: html for role, html in read_fixture_pages(case_dir)}
    assert "home" in pages, f"missing home page for {row['case_id']}"
    eng = _run_load_into(tmp_path, monkeypatch, row["website"], pages)
    column = _FIELD_TO_COLUMN[row["field"]]
    with Session(eng) as s:
        stored = s.execute(
            text(f"SELECT {column} AS v FROM website_enrichment WHERE website = :w"),
            {"w": row["website"]},
        ).scalar_one()
    expected = row["expected_value"]
    if column in ("attorney_count_min", "office_count", "years_in_operation_min"):
        assert stored == int(expected)
    elif column == "is_law_related":
        assert bool(stored) is (expected.strip().lower() in ("true", "1", "yes"))
    else:
        assert (str(stored or "").lower()) == expected.strip().lower()
