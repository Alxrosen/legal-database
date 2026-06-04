"""Generic state-bar scrape pipeline (config-driven, one per-state config).

Flow (the AZ Bar list->detail shape, generalized):

    1. SWEEP   enumerate the directory by the config's strategy (last-name
               prefixes a..z, etc.), fetching each search's result page(s).
    2. LIST    parse each results page into rows (attorney + detail URL),
               persisting a per-term ``list_index/{term}.json``.
    3. DETAIL  fetch each attorney's detail page (firm / address / phone live
               there). Skip already-fetched files; checkpoint per sweep term.
    4. BUILD   parse every stored detail (+ its list row) into a record,
               normalize, aggregate attorneys into firm rows, upsert.

Crash-safety: the checkpoint (one sweep term = one durable unit) avoids
re-fetching completed terms; the global BUILD reads every stored detail
across date partitions, so a crash in parse/upsert is recovered by ``load``
(no network). Mirrors ``scrape_az_bar`` and the national ``full`` runs.

CLI:
    uv run python -m legal_sourcing.pipelines.scrape_state_bar pilot --state wy
    uv run python -m legal_sourcing.pipelines.scrape_state_bar full  --state wy
    uv run python -m legal_sourcing.pipelines.scrape_state_bar load  --state wy
"""

from __future__ import annotations

import argparse
import gzip
import json
import string
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from legal_sourcing.config import get_settings
from legal_sourcing.db import make_engine
from legal_sourcing.parsers.state_bar import parse_detail, parse_list
from legal_sourcing.pipelines._checkpoint import Checkpoint
from legal_sourcing.pipelines.scrape_az_bar import (
    aggregate_by_firm,
    normalize_record,
    upsert_firm_source_records,
)
from legal_sourcing.scrapers.state_bar import StateBarScraper
from legal_sourcing.state_bars import StateBarConfig, get_config
from legal_sourcing.utils.logging import configure_logging, get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Sweep strategy


def sweep_terms(cfg: StateBarConfig) -> list[str]:
    if cfg.sweep == "single":
        return [""]
    if cfg.sweep == "alpha":
        return list(string.ascii_lowercase)
    if cfg.sweep == "alpha2":
        return [a + b for a in string.ascii_lowercase for b in string.ascii_lowercase]
    raise ValueError(f"unknown sweep {cfg.sweep!r}")


# ---------------------------------------------------------------------------
# Raw IO helpers


def _read_gz(path: Path) -> bytes:
    with gzip.open(path, "rb") as f:
        return f.read()


def _sidecar(gz_path: Path) -> dict[str, Any]:
    sc = gz_path.with_suffix("").with_suffix(".json")
    try:
        return json.loads(sc.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Fetch phases


def fetch_term_list(
    scraper: StateBarScraper, cfg: StateBarConfig, term: str
) -> list[dict[str, Any]]:
    """Fetch + parse all result pages for one sweep term. Returns the list
    rows (deduped by detail id). Raw pages stored under list/."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1
    range_start = 1
    for _ in range(cfg.max_pages_per_search):
        fields = dict(cfg.static_fields)
        if cfg.search_field and term:
            fields[cfg.search_field] = term
        if cfg.pagination == "page" and cfg.page_field:
            fields[cfg.page_field] = str(page)
        elif cfg.pagination == "range" and cfg.page_field:
            fields[cfg.page_field] = f"{range_start}/{cfg.page_size}"

        method = cfg.list_method.upper()
        kw: dict[str, Any] = {"params": fields} if method == "GET" else {"data": fields}
        path = scraper.fetch_one(
            cfg.list_url, method=method, bucket="list", filename=f"{term or 'all'}_p{page}", **kw
        )
        if path is None:
            break
        page_rows = parse_list(cfg.list_extractor, _read_gz(path), cfg.base_url)
        new = [r for r in page_rows if r["detail_id"] not in seen]
        for r in new:
            seen.add(r["detail_id"])
        rows.extend(new)

        if cfg.pagination == "none" or not page_rows or not new:
            break
        page += 1
        range_start += cfg.page_size or len(page_rows)
    if len(rows) >= 240:
        # heuristic cap warning — many bar systems silently cap a query
        log.warning("state_bar.large_term", source=cfg.source, term=term, rows=len(rows))
    return rows


def _detail_path(scraper: StateBarScraper, detail_id: str) -> Path:
    # mirror BaseScraper's date partition + bucket; html content -> .html.gz
    from datetime import date

    base = get_settings().raw_data_dir / scraper.SOURCE_NAME / date.today().isoformat() / "detail"
    return base / f"{detail_id}.html.gz"


def fetch_details(
    scraper: StateBarScraper, rows: list[dict[str, Any]], *, max_details: int | None = None
) -> int:
    """Fetch each row's detail page (skip already-present). Returns count fetched."""
    fetched = 0
    for i, r in enumerate(rows):
        if max_details is not None and i >= max_details:
            break
        if _detail_path(scraper, r["detail_id"]).exists():
            continue
        try:
            scraper.fetch_one(
                r["detail_url"], method="GET", bucket="detail", filename=r["detail_id"]
            )
            fetched += 1
        except Exception as exc:
            log.error("state_bar.detail_failed", id=r["detail_id"], error=str(exc))
    return fetched


# ---------------------------------------------------------------------------
# Build (parse -> normalize -> aggregate -> upsert)


def _build_record(
    cfg: StateBarConfig, row: dict[str, Any], detail_path: Path
) -> dict[str, Any] | None:
    if cfg.detail_extractor is None:
        return None
    sidecar = _sidecar(detail_path)
    rec = parse_detail(
        cfg.detail_extractor, _read_gz(detail_path), base_url=cfg.base_url, list_row=row
    )
    rec["source"] = cfg.source
    rec.setdefault("source_url", row.get("detail_url") or cfg.base_url)
    rec["scraped_at"] = sidecar.get("fetched_at")
    rec["raw_payload_path"] = str(detail_path)
    rec["http_status"] = sidecar.get("status")
    return rec


def _collect_rows_and_details(cfg: StateBarConfig) -> list[tuple[dict[str, Any], Path]]:
    """Across every date partition: pair each persisted list row with its
    stored detail file (latest found wins). Used by the global build/load."""
    raw_root = get_settings().raw_data_dir / cfg.source
    if not raw_root.exists():
        return []
    rows_by_id: dict[str, dict[str, Any]] = {}
    for idx in sorted(raw_root.glob("*/list_index/*.json")):
        try:
            for r in json.loads(idx.read_text(encoding="utf-8")):
                rows_by_id[r["detail_id"]] = r
        except (OSError, json.JSONDecodeError):
            continue
    detail_by_id: dict[str, Path] = {}
    for p in sorted(raw_root.glob("*/detail/*.html.gz")):
        detail_by_id[p.name[: -len(".html.gz")]] = p
    out: list[tuple[dict[str, Any], Path]] = []
    for did, row in rows_by_id.items():
        p = detail_by_id.get(did)
        if p is not None:
            out.append((row, p))
    return out


def _build_and_upsert(cfg: StateBarConfig, pairs: list[tuple[dict[str, Any], Path]]) -> None:
    records: list[dict[str, Any]] = []
    for row, path in pairs:
        rec = _build_record(cfg, row, path)
        if rec is not None:
            records.append(rec)
    for r in records:
        normalize_record(r)
    firm_records = aggregate_by_firm(records)
    log.info(
        "state_bar.aggregate_done",
        source=cfg.source,
        attorneys=len(records),
        firms=len(firm_records),
    )
    engine = make_engine()
    with Session(engine) as session:
        counts = upsert_firm_source_records(session, firm_records)
    log.info("state_bar.upsert_done", source=cfg.source, **counts)
    print(
        f"{cfg.source}: {len(records)} attorneys -> {len(firm_records)} firms "
        f"({counts['inserted']} inserted, {counts['updated']} updated)."
    )


def _write_list_index(scraper: StateBarScraper, term: str, rows: list[dict[str, Any]]) -> None:
    from datetime import date

    d = get_settings().raw_data_dir / scraper.SOURCE_NAME / date.today().isoformat() / "list_index"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{term or 'all'}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Orchestrators


def run_full(state: str, *, resume: bool = True) -> None:
    cfg = get_config(state)
    configure_logging()
    ckpt = Checkpoint(f"{cfg.source}_full")
    terms = sweep_terms(cfg)
    with StateBarScraper(cfg) as scraper:
        log.info("state_bar.full_start", source=cfg.source, terms=len(terms))
        for term in terms:
            if resume and ckpt.is_done(term):
                continue
            rows = fetch_term_list(scraper, cfg, term)
            _write_list_index(scraper, term, rows)
            fetched = fetch_details(scraper, rows)
            ckpt.mark_done(term)
            log.info(
                "state_bar.term_done",
                source=cfg.source,
                term=term,
                rows=len(rows),
                fetched=fetched,
                terms_done=ckpt.completed_count,
            )
    # Global build from everything on disk (recoverable via `load`).
    _build_and_upsert(cfg, _collect_rows_and_details(cfg))
    log.info("state_bar.full_done", source=cfg.source)


def run_load(state: str) -> None:
    cfg = get_config(state)
    configure_logging()
    pairs = _collect_rows_and_details(cfg)
    log.info("state_bar.load_start", source=cfg.source, details=len(pairs))
    _build_and_upsert(cfg, pairs)


def run_pilot(state: str, *, terms: list[str] | None = None, max_details: int = 40) -> None:
    cfg = get_config(state)
    configure_logging()
    chosen = terms or sweep_terms(cfg)[:1]
    with StateBarScraper(cfg) as scraper:
        log.info("state_bar.pilot_start", source=cfg.source, terms=chosen, max_details=max_details)
        all_rows: list[dict[str, Any]] = []
        for term in chosen:
            rows = fetch_term_list(scraper, cfg, term)
            _write_list_index(scraper, term, rows)
            all_rows.extend(rows)
        log.info("state_bar.pilot_list_done", source=cfg.source, rows=len(all_rows))
        fetch_details(scraper, all_rows, max_details=max_details)
    # Build only from the rows we just fetched (capped).
    pairs: list[tuple[dict[str, Any], Path]] = []
    for r in all_rows[:max_details]:
        p = _detail_path(scraper, r["detail_id"])
        if p.exists():
            pairs.append((r, p))
    _build_and_upsert(cfg, pairs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("pilot", "full", "load"))
    parser.add_argument("--state", required=True, help="state abbr, e.g. wy")
    parser.add_argument("--terms", type=str, default=None, help="(pilot) comma list of sweep terms")
    parser.add_argument("--max-details", type=int, default=40, help="(pilot) cap detail fetches")
    parser.add_argument("--no-resume", action="store_true", help="(full) ignore checkpoint")
    args = parser.parse_args()

    if args.mode == "pilot":
        terms = [t.strip().lower() for t in args.terms.split(",")] if args.terms else None
        run_pilot(args.state, terms=terms, max_details=args.max_details)
    elif args.mode == "load":
        run_load(args.state)
    else:
        run_full(args.state, resume=not args.no_resume)
    return 0


if __name__ == "__main__":
    sys.exit(main())
