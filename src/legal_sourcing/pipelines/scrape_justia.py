"""Justia Lawyer Directory scrape pipeline.

Justia is an ATTORNEY directory with NO firm name on its listing cards
(see docs/data_sources/justia.md). So unlike the other sources we
aggregate lawyers into firm-shaped records by **normalized office
(street, city)** rather than by firm name: lawyers sharing an office
become one FirmSourceRecord (`name_raw=None`, those lawyers as
contacts, practice areas unioned). A lawyer with no parseable street
stays a solo record keyed on the Justia profile id. Cross-source
resolution matches Justia records to named firms via phone / website /
address.

`full` walks each state's directory page-by-page (following the "Next"
link), committing one state at a time with a resumable checkpoint —
same crash-safety model as Martindale/FindLaw. State pagination is
capped by Justia (high pages redirect off the `lawyers.` subdomain); we
detect that redirect and stop. `load` re-parses fetched pages from disk.

CLI::

    uv run python -m legal_sourcing.pipelines.scrape_justia full --states all
    uv run python -m legal_sourcing.pipelines.scrape_justia load [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from legal_sourcing.config import get_settings
from legal_sourcing.db import make_engine
from legal_sourcing.geo import parse_states_arg
from legal_sourcing.parsers.justia import JustiaDirectoryParser, extract_page_meta
from legal_sourcing.pipelines._checkpoint import Checkpoint
from legal_sourcing.pipelines.scrape_az_bar import (
    normalize_record,
    upsert_firm_source_records,
)
from legal_sourcing.scrapers.base import ScrapeError
from legal_sourcing.scrapers.justia import JustiaCloudflareChallenge, JustiaScraper
from legal_sourcing.utils.logging import configure_logging, get_logger

log = get_logger(__name__)

_PAGE_HARD_CAP = 120  # safety net well past Justia's own state cap


def _read_gz_bytes(path: Path) -> bytes:
    with gzip.open(path, "rb") as f:
        return f.read()


def _final_url(gz_path: Path) -> str | None:
    """Read the stored sidecar's final_url for a fetched page."""
    sidecar = gz_path.parent / (gz_path.name.split(".", 1)[0] + ".json")
    try:
        return json.loads(sidecar.read_text(encoding="utf-8")).get("final_url")
    except (OSError, json.JSONDecodeError):
        return None


def fetch_state_pages(
    scraper: JustiaScraper, state_slug: str, *, max_pages: int | None = None
) -> list[Path]:
    """Walk a state's directory pages page-by-page.

    Justia caps per-state pagination: beyond the cap, a `?page=N` request
    is redirected to the page-1 hub with the page param dropped (and the
    hub still shows a misleading "Next" link, so has_next is NOT a
    reliable cap signal). Stop signals: the page param dropped on
    redirect (the real cap), an empty page, no Next link, max_pages, or
    the hard cap.
    """
    paths: list[Path] = []
    page = 1
    while True:
        url = scraper.state_url(state_slug, page=page)
        try:
            path = scraper.fetch_one(
                url, method="GET", bucket=f"state/{state_slug}", filename=f"p{page:03d}"
            )
        except ScrapeError as exc:
            log.warning(
                "justia.page_fetch_failed",
                state=state_slug,
                page=page,
                error=str(exc)[:200],
            )
            break
        if path is None:
            break

        # Cap detection: past the per-state cap, ?page=N is redirected to
        # the page-1 hub with the param dropped. For page>1, a missing
        # `page={page}` in the final URL means we hit the cap — stop
        # without counting the hub page (it's page-1 data, not page N).
        final = _final_url(path) or ""
        if page > 1 and f"page={page}" not in final:
            log.info(
                "justia.pagination_cap",
                state=state_slug,
                page=page,
                final_url=final,
            )
            break

        body = _read_gz_bytes(path).decode("utf-8", errors="replace")
        meta = extract_page_meta(body)
        if meta["card_count"] == 0:
            if page == 1:
                paths.append(path)  # keep page 1 even if empty (for load)
            break
        paths.append(path)

        if max_pages is not None and page >= max_pages:
            break
        if not meta["has_next"]:
            break
        page += 1
        if page > _PAGE_HARD_CAP:
            log.warning("justia.page_hard_cap", state=state_slug, cap=_PAGE_HARD_CAP)
            break
    log.info("justia.state_done", state=state_slug, pages=len(paths))
    return paths


def parse_paths(paths: list[Path]) -> list[dict[str, Any]]:
    parser = JustiaDirectoryParser()
    records: list[dict[str, Any]] = []
    for p in paths:
        records.extend(parser.parse_file(p))
    return records


def aggregate_by_office(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group per-lawyer records into firm-shaped records by normalized
    (street, city). No street -> solo record keyed on the profile id."""
    groups: dict[tuple, dict[str, Any]] = {}
    for r in records:
        offices = r.get("offices") or []
        norm = (offices[0].get("normalized") if offices else None) or {}
        street, city = norm.get("street"), norm.get("city")
        if street:
            key: tuple = ("office", street, city)
        else:
            pid = (r.get("additional_data") or {}).get("source_profile_id_justia")
            key = ("solo", pid if pid is not None else id(r), None)

        if key not in groups:
            seed = {k: v for k, v in r.items() if k not in ("contacts", "offices")}
            seed["contacts"] = list(r.get("contacts", []))
            seed["offices"] = list(r.get("offices", []))
            seed["practice_areas_raw"] = list(r.get("practice_areas_raw") or [])
            seed["practice_areas_matched"] = list(r.get("practice_areas_matched") or [])
            seed["practice_areas_unmatched"] = list(r.get("practice_areas_unmatched") or [])
            seed["additional_data"] = dict(r.get("additional_data") or {})
            groups[key] = seed
            continue

        agg = groups[key]
        agg["contacts"].extend(r.get("contacts", []))
        seen = {(o.get("normalized") or {}).get("street") for o in agg["offices"]}
        for o in r.get("offices", []):
            s = (o.get("normalized") or {}).get("street")
            if s not in seen:
                agg["offices"].append(o)
                seen.add(s)
        for fld in ("website_raw", "website_normalized", "phone_raw", "phone_normalized"):
            if not agg.get(fld):
                agg[fld] = r.get(fld)
        for col in ("practice_areas_raw", "practice_areas_matched", "practice_areas_unmatched"):
            agg[col] = sorted(set(agg[col]) | set(r.get(col) or []))
        for k, v in (r.get("additional_data") or {}).items():
            agg["additional_data"].setdefault(k, v)

    result: list[dict[str, Any]] = []
    for key, rec in groups.items():
        rec["attorney_count"] = len(rec["contacts"])
        ident = f"justia|{key[1]}|{key[2] or ''}" if key[0] == "office" else f"justia|solo|{key[1]}"
        rec["source_firm_id"] = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]
        result.append(rec)
    return result


def _tag_provenance(firm_records: list[dict[str, Any]], *, source_url: str) -> None:
    now = dt.datetime.now(dt.UTC)
    for r in firm_records:
        r.setdefault("source", "justia")
        r.setdefault("scraped_at", now)
        r.setdefault("source_url", source_url)


def _process_state(
    state_slug: str, paths: list[Path], engine: Any, source_url: str
) -> dict[str, int]:
    records = parse_paths(paths)
    for r in records:
        normalize_record(r)
    firm_records = aggregate_by_office(records)
    _tag_provenance(firm_records, source_url=source_url)
    with Session(engine) as session:
        counts = upsert_firm_source_records(session, firm_records)
    log.info(
        "justia.state_upserted",
        state=state_slug,
        lawyers=len(records),
        firms=len(firm_records),
        **counts,
    )
    return counts


def run_full(
    *,
    states: list[str] | None = None,
    max_pages_per_state: int | None = None,
    resume: bool = True,
) -> None:
    configure_logging()
    states = states if states is not None else parse_states_arg("all")
    cp = Checkpoint("justia_full")
    log.info(
        "justia.full_start",
        states=len(states),
        max_pages_per_state=max_pages_per_state,
        resume=resume,
        already_done=cp.completed_count,
    )
    engine = make_engine()
    try:
        with JustiaScraper() as scraper:
            for state_slug in states:
                if resume and cp.is_done(state_slug):
                    continue
                paths = fetch_state_pages(scraper, state_slug, max_pages=max_pages_per_state)
                if not paths:
                    cp.mark_done(state_slug)
                    continue
                counts = _process_state(state_slug, paths, engine, scraper.state_url(state_slug))
                cp.mark_done(state_slug, inserted=counts["inserted"], updated=counts["updated"])
    except JustiaCloudflareChallenge as exc:
        log.error("justia.cloudflare_abort", error=str(exc)[:300])
        print(
            f"STOPPED on Cloudflare challenge: {exc}\nCheckpoint saved "
            f"({cp.completed_count} states). Re-run to resume.",
            file=sys.stderr,
        )
        return

    t = cp.totals
    log.info("justia.full_done", **t)
    print(
        f"Justia full complete: {t['cities']} states committed "
        f"({t['inserted']} inserted, {t['updated']} updated total)."
    )


def run_load(*, date_str: str | None = None, resume: bool = False) -> None:
    settings = get_settings()
    configure_logging()
    base = settings.raw_data_dir / "justia"
    if not base.exists():
        print(f"No Justia raw data at {base}", file=sys.stderr)
        return
    if date_str is None:
        date_dirs = sorted([p for p in base.iterdir() if p.is_dir()])
        if not date_dirs:
            print(f"No date partitions under {base}", file=sys.stderr)
            return
        date_str = date_dirs[-1].name
    state_root = base / date_str / "state"
    if not state_root.exists():
        print(f"No state/ dir at {state_root}", file=sys.stderr)
        return

    groups: dict[str, list[Path]] = {}
    for gz in state_root.glob("*/*.html.gz"):
        groups.setdefault(gz.parent.name, []).append(gz)

    log.info("justia.load_start", date=date_str, states=len(groups), resume=resume)
    cp = Checkpoint("justia_load") if resume else None
    engine = make_engine()
    total = {"inserted": 0, "updated": 0, "states": 0}
    for state_slug, paths in sorted(groups.items()):
        if cp is not None and cp.is_done(state_slug):
            continue
        counts = _process_state(
            state_slug,
            sorted(paths),
            engine,
            f"https://lawyers.justia.com/lawyers/{state_slug}",
        )
        total["inserted"] += counts["inserted"]
        total["updated"] += counts["updated"]
        total["states"] += 1
        if cp is not None:
            cp.mark_done(state_slug, inserted=counts["inserted"], updated=counts["updated"])
    log.info("justia.load_done", **total)
    print(
        f"Justia load complete: {total['states']} states "
        f"({total['inserted']} inserted, {total['updated']} updated)."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("full", "load"),
        help="full = national state sweep (resumable). load = re-parse "
        "fetched pages from disk (no network).",
    )
    parser.add_argument("--states", type=str, default="all")
    parser.add_argument(
        "--max-pages-per-state",
        type=int,
        default=0,
        help="Cap pages per state (0 = follow Next to Justia's own cap).",
    )
    parser.add_argument("--date", type=str, default=None)
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    cap = args.max_pages_per_state if args.max_pages_per_state > 0 else None
    if args.mode == "full":
        run_full(
            states=parse_states_arg(args.states),
            max_pages_per_state=cap,
            resume=not args.no_resume,
        )
    elif args.mode == "load":
        run_load(date_str=args.date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
