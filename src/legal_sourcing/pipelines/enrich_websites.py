"""Firm-website enrichment pipeline — the background "producer/worker" program.

Producer = the set-difference of distinct `firm_source_records.website_normalized`
not yet enriched (or stale). Workers (a thread pool) each crawl ONE firm
sequentially — home page -> discovered about/team/attorney pages — run the
extraction cascade (enrichment/website_extract.py), and hand a result row back.
A SINGLE committer (the main thread) buffers rows and BULK-upserts them
periodically (SQLite ON CONFLICT DO UPDATE). See docs/assumptions.md 2026-06-03
"concurrent-scrape durability": raw bytes hit disk per fetch (crash backstop +
no-re-scrape), the in-memory buffer holds only cheap-to-recompute rows, one
writer keeps lock contention low alongside the Martindale run.

CLI:
    uv run python -m legal_sourcing.pipelines.enrich_websites pilot --websites Goetzlaw.com,bipc.com
    uv run python -m legal_sourcing.pipelines.enrich_websites pilot --limit 30
    uv run python -m legal_sourcing.pipelines.enrich_websites run [--limit N] [--workers 6]
    uv run python -m legal_sourcing.pipelines.enrich_websites load   # re-extract from disk, no network
    uv run python -m legal_sourcing.pipelines.enrich_websites load-fsr [--limit N]  # -> firm_source_records (source="website"); --limit 0 = full
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import distinct, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from legal_sourcing.config import get_settings
from legal_sourcing.db import make_engine
from legal_sourcing.enrichment.website_extract import (
    SiteExtraction,
    discover_internal_pages,
    extract_site,
)
from legal_sourcing.models import FirmSourceRecord, WebsiteEnrichment
from legal_sourcing.normalize.url import is_aggregator_domain
from legal_sourcing.pipelines.scrape_az_bar import normalize_record
from legal_sourcing.scrapers.base import ScrapeError
from legal_sourcing.scrapers.website import FirmWebsiteScraper
from legal_sourcing.utils.logging import configure_logging, get_logger

log = get_logger(__name__)

_UPDATE_SKIP = ("id", "website", "created_at")

# Every row dict MUST carry the same keys (a bulk INSERT ... VALUES needs
# homogeneous dicts), and the NOT-NULL boolean columns must never be None.
_ROW_DEFAULTS: dict[str, Any] = {
    "resolved_url": None,
    "platform": None,
    "pages_crawled": None,
    "is_law_related": None,
    "relevance_terms": None,
    "attorney_count_min": None,
    "attorney_count_is_min": False,
    "attorney_count_method": None,
    "attorney_count_confidence": None,
    "attorney_count_raw": None,
    "staff_count_min": None,
    "office_count": None,
    "office_addresses": None,
    "primary_city": None,
    "primary_state": None,
    "years_in_operation_min": None,
    "years_is_min": False,
    "scope": None,
    "notable_signals": None,
    "practice_areas": None,
    "practice_areas_raw": None,
    "phones": None,
    "description_blurb": None,
    "description_generated": None,
    "url_verification_status": None,
    "url_verification_score": None,
    "needs_render": False,
    "http_status": None,
    "raw_html_path": None,
    "fetched_at": None,
    "enriched_at": None,
}


# ---------------------------------------------------------------------------
# Raw IO


def _bucket(website: str) -> str:
    slug = re.sub(r"[^a-z0-9.-]", "_", website.lower())[:50]
    return f"{slug}_{hashlib.sha256(website.encode()).hexdigest()[:8]}"


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
# Row construction


def _row_from_site(
    website: str,
    site: SiteExtraction,
    *,
    resolved_url: str | None,
    http_status: int | None,
    raw_html_path: str | None,
    pages_meta: list[dict[str, str]],
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        **_ROW_DEFAULTS,
        "website": website,
        "resolved_url": resolved_url,
        "platform": site.platform,
        "pages_crawled": pages_meta,
        "is_law_related": site.is_law_related,
        "relevance_terms": site.relevance_terms,
        "attorney_count_min": site.attorney_count,
        "attorney_count_is_min": site.attorney_count_is_min,
        "attorney_count_method": site.attorney_count_method,
        "attorney_count_confidence": site.attorney_count_confidence,
        "attorney_count_raw": site.attorney_count_raw,
        "staff_count_min": site.staff_count,
        "office_count": site.office_count,
        "office_addresses": site.office_addresses,
        "primary_city": site.primary_city,
        "primary_state": site.primary_state,
        "years_in_operation_min": site.years_in_operation,
        "years_is_min": site.years_is_min,
        "scope": site.scope,
        "notable_signals": site.notable_signals,
        "practice_areas": site.practice_areas,
        "practice_areas_raw": site.practice_areas_raw,
        "phones": site.phones,
        "description_blurb": site.description_blurb,
        "url_verification_status": site.url_verification_status,
        "needs_render": site.needs_render,
        "http_status": http_status,
        "raw_html_path": raw_html_path,
        "fetched_at": now,
        "enriched_at": now,
    }


def _unreachable_row(website: str, *, http_status: int | None = None) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        **_ROW_DEFAULTS,
        "website": website,
        "url_verification_status": "unreachable",
        "http_status": http_status,
        "fetched_at": now,
        "enriched_at": now,
    }


# ---------------------------------------------------------------------------
# Worker: crawl one firm (runs in a thread; NO DB access here)


def crawl_firm(
    scraper: FirmWebsiteScraper, website: str, *, max_sub_pages: int = 5
) -> dict[str, Any]:
    bucket = _bucket(website)
    home_path: Path | None = None
    base: str | None = None
    resolved_url: str | None = None
    http_status: int | None = None

    for scheme in ("https://", "http://"):
        candidate = scheme + website
        try:
            p = scraper.fetch_one(candidate, bucket=bucket, filename="home")
        except ScrapeError:
            continue
        except Exception as exc:
            log.warning("enrich.home_error", website=website, error=str(exc))
            continue
        if p is not None:
            home_path, base = p, candidate
            sc = _sidecar(p)
            resolved_url = sc.get("final_url") or candidate
            http_status = sc.get("status")
            break

    if home_path is None:
        return _unreachable_row(website)

    home_html = _read_gz(home_path)
    pages: list[tuple[str, bytes]] = [("home", home_html)]
    pages_meta: list[dict[str, str]] = [{"role": "home", "url": base or ""}]

    disc = discover_internal_pages(home_html, resolved_url or base or "")
    to_fetch: list[tuple[str, str]] = (
        [("attorneys", u) for u in disc["attorneys"][:3]]
        + [("team", u) for u in disc["team"][:1]]
        + [("about", u) for u in disc["about"][:1]]
    )[:max_sub_pages]
    for i, (role, url) in enumerate(to_fetch):
        try:
            p = scraper.fetch_one(url, bucket=bucket, filename=f"{role}{i}")
        except Exception:  # a missing/blocked sub-page is non-fatal
            continue
        if p is not None:
            pages.append((role, _read_gz(p)))
            pages_meta.append({"role": role, "url": url})

    site = extract_site(pages, base_url=base or "")
    return _row_from_site(
        website,
        site,
        resolved_url=resolved_url,
        http_status=http_status,
        raw_html_path=str(home_path),
        pages_meta=pages_meta,
    )


# ---------------------------------------------------------------------------
# Producer + committer


def websites_to_enrich(engine, *, limit: int | None = None) -> list[str]:
    """Distinct, valid, non-aggregator websites not yet enriched (set-difference)."""
    with Session(engine) as s:
        done = set(
            s.scalars(
                select(WebsiteEnrichment.website).where(WebsiteEnrichment.enriched_at.isnot(None))
            ).all()
        )
        rows = s.execute(
            select(distinct(FirmSourceRecord.website_normalized)).where(
                FirmSourceRecord.website_normalized.isnot(None),
                FirmSourceRecord.website_normalized != "",
            )
        ).all()
    out: list[str] = []
    for (w,) in rows:
        # Skip already-done, aggregator domains, and email-as-website junk
        # (~154 `FirmURL` values are emails like "x@gmail.com"; crawling them
        # just hits the mail host and writes a bogus row). The upstream
        # normalize_url @-guard is in Mastermind's lane; this is a producer
        # backstop so the enrichment run never wastes a fetch on one.
        if not w or w in done or "@" in w or is_aggregator_domain(w):
            continue
        out.append(w)
        if limit and len(out) >= limit:
            break
    return out


def _flush(engine, rows: list[dict[str, Any]]) -> int:
    """Bulk upsert a batch (single writer). Retries on transient SQLite lock."""
    if not rows:
        return 0
    cols = [c.name for c in WebsiteEnrichment.__table__.columns if c.name not in _UPDATE_SKIP]
    for attempt in range(6):
        try:
            with engine.begin() as conn:
                stmt = sqlite_insert(WebsiteEnrichment).values(rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["website"],
                    set_={c: getattr(stmt.excluded, c) for c in cols},
                )
                conn.execute(stmt)
            return len(rows)
        except OperationalError:
            if attempt == 5:
                raise
            time.sleep(0.5 * (2**attempt))
    return 0


def _crawl_all(
    websites: list[str], *, workers: int, flush_every: int, print_each: bool = False
) -> list[dict[str, Any]]:
    engine = make_engine()
    buffer: list[dict[str, Any]] = []
    collected: list[dict[str, Any]] = []
    done = 0
    with FirmWebsiteScraper() as scraper, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(crawl_firm, scraper, w): w for w in websites}
        for fut in as_completed(futures):
            w = futures[fut]
            try:
                row = fut.result()
            except Exception as exc:
                log.error("enrich.worker_failed", website=w, error=str(exc))
                row = _unreachable_row(w)
            buffer.append(row)
            collected.append(row)
            done += 1
            if print_each:
                _print_row(row)
            if len(buffer) >= flush_every:
                n = _flush(engine, buffer)
                log.info("enrich.flush", flushed=n, total_done=done)
                buffer.clear()
        if buffer:
            n = _flush(engine, buffer)
            log.info("enrich.flush", flushed=n, total_done=done)
    return collected


def _print_row(row: dict[str, Any]) -> None:
    ac = row.get("attorney_count_min")
    method = row.get("attorney_count_method") or "-"
    print(
        f"  {row['website'][:34]:34s} {row.get('url_verification_status', ''):20s} "
        f"atty={ac!s:>5} ({method:13s}) staff={row.get('staff_count_min')!s:>4} "
        f"off={row.get('office_count')!s:>3} yrs={row.get('years_in_operation_min')!s:>3} "
        f"plat={row.get('platform') or '-':11s} render={row.get('needs_render')}"
    )


# ---------------------------------------------------------------------------
# Orchestrators


def run_pilot(*, websites: list[str] | None, limit: int, workers: int) -> None:
    configure_logging()
    if not websites:
        engine = make_engine()
        websites = websites_to_enrich(engine, limit=limit)
    print(f"== website-enrichment pilot: {len(websites)} firms, {workers} workers ==")
    print(
        f"  {'website':34s} {'status':20s} {'attorneys':>5} {'method':15s} staff off yrs platform render"
    )
    _crawl_all(websites, workers=workers, flush_every=20, print_each=True)
    print(f"\nWrote {len(websites)} rows to website_enrichment.")


def run(*, limit: int | None, workers: int, flush_every: int) -> None:
    configure_logging()
    engine = make_engine()
    websites = websites_to_enrich(engine, limit=limit)
    log.info("enrich.run_start", to_enrich=len(websites), workers=workers)
    _crawl_all(websites, workers=workers, flush_every=flush_every)
    log.info("enrich.run_done", count=len(websites))
    print(f"Enriched {len(websites)} websites.")


def run_load(*, flush_every: int = 400) -> None:
    """Re-extract from already-fetched raw pages on disk (NO network).

    Flushes in batches so the full corpus (tens of thousands of sites) stays well
    under SQLite's bound-parameter limit — a single all-rows INSERT would exceed
    it. Idempotent (upsert keyed by website); safe to re-run after a parser fix.
    """
    configure_logging()
    settings = get_settings()
    base = settings.raw_data_dir / "firm_websites"
    if not base.exists():
        print(f"No firm_websites raw data at {base}", file=sys.stderr)
        return
    # Group stored pages by bucket dir (across date partitions; latest wins).
    buckets: dict[str, dict[str, Path]] = {}
    for gz in sorted(base.glob("*/*/*.html.gz")):
        buckets.setdefault(gz.parent.name, {})[gz.stem.replace(".html", "")] = gz
    engine = make_engine()
    buffer: list[dict[str, Any]] = []
    total = 0
    for files in buckets.values():
        home = files.get("home")
        if home is None:
            continue
        sc = _sidecar(home)
        url = sc.get("url") or ""
        website = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
        if not website:
            continue
        pages: list[tuple[str, bytes]] = []
        for name, path in sorted(files.items()):
            role = "home" if name == "home" else re.sub(r"\d+$", "", name)
            pages.append((role, _read_gz(path)))
        site = extract_site(pages, base_url=url)
        buffer.append(
            _row_from_site(
                website,
                site,
                resolved_url=sc.get("final_url"),
                http_status=sc.get("status"),
                raw_html_path=str(home),
                pages_meta=[{"role": "home", "url": url}],
            )
        )
        if len(buffer) >= flush_every:
            total += _flush(engine, buffer)
            log.info("enrich.load_flush", upserted=total)
            buffer.clear()
    if buffer:
        total += _flush(engine, buffer)
    print(f"load: re-extracted + upserted {total} websites from disk.")


# ---------------------------------------------------------------------------
# Website-as-a-source: firm_source_records (source="website")
#
# Per COORDINATION 2026-06-08 14:19 (Mastermind), the website is a first-class
# MDM SOURCE row in firm_source_records — NOT a widened website_enrichment. We
# re-extract the cached raw (no re-fetch) and upsert one FirmSourceRecord per
# crawled domain so the website votes as a regular cluster member in canonical
# resolution (naming the otherwise-nameless Justia-only firms via the
# website-identity floor). website_enrichment stays the per-domain crawl cache.

# Site verdicts that represent a real firm site worth emitting as a source row.
# not_a_law_firm / unreachable / government_or_edu are NOT acquirable firms;
# emitting them would fabricate junk records. The verdict is still recorded in
# additional_data on the rows we DO emit.
_FSR_EMIT_STATUSES = ("verified", "legal_but_mismatched")

# FirmSourceRecord columns the website source writes (the FULL field set). The
# generic martindale upsert (scrape_az_bar.upsert_firm_source_records) omits
# primary_*/office_count/firm_short_description/firm_descriptions AND blindly
# overwrites every column on update — so reusing it would NULL those on a
# martindale re-run. The website load therefore uses its own upsert below, which
# overwrites the full set on re-extract (correct for our own source).
_FSR_COLUMNS = (
    "source",
    "source_firm_id",
    "source_url",
    "scraped_at",
    "raw_payload_path",
    "http_status",
    "name_raw",
    "name_normalized",
    "website_raw",
    "website_normalized",
    "phone_raw",
    "phone_normalized",
    "year_founded",
    "attorney_count",
    "deactivation_status",
    "primary_city",
    "primary_state",
    "primary_postal_code",
    "office_count",
    "firm_short_description",
    "firm_descriptions",
    "contacts",
    "offices",
    "practice_areas_raw",
    "practice_areas_matched",
    "practice_areas_unmatched",
    "additional_data",
)


def fsr_record_from_site(
    website: str,
    site: SiteExtraction,
    *,
    source_url: str,
    http_status: int | None,
    raw_payload_path: str | None,
    scraped_at: datetime,
) -> dict[str, Any] | None:
    """Map a SiteExtraction -> a FirmSourceRecord upsert dict (source="website").

    Returns None for sites that aren't real firm pages (so we never fabricate a
    firm). Raw fields are populated here; the shared `normalize_record` then derives
    the *_normalized merge keys (name/website/phone, office normalization, practice-
    area matched/unmatched) EXACTLY as the martindale loader does — so a website row
    keys identically to a firm's other source rows and merges via the website /
    name+city+state floors.
    """
    if site.url_verification_status not in _FSR_EMIT_STATUSES:
        return None

    offices: list[dict[str, Any]] = [
        {
            "city_raw": a.get("city"),
            "state_raw": a.get("state"),
            "postal_code_raw": a.get("postal_code"),
            "is_primary": i == 0,
        }
        for i, a in enumerate(site.office_addresses)
    ]

    # All advertised practice-area phrases, verbatim (taxonomy-matched ones plus the
    # URL-declared unmatched ones). normalize_record re-splits them into canonical
    # matched slugs + normalized unmatched, identically to every other source.
    practice_raw = list(site.practice_areas_raw) + list(site.practice_areas_unmatched)

    additional: dict[str, Any] = {
        "enrichment_source": "website",
        "url_verification_status": site.url_verification_status,
        "platform": site.platform,
        "needs_render": site.needs_render,
        "is_law_related": site.is_law_related,
    }
    if site.scope:
        additional["scope"] = site.scope
    if site.notable_signals:
        additional["notable_signals"] = list(site.notable_signals)
    if site.attorney_count is not None:
        additional["attorney_count_method"] = site.attorney_count_method
        additional["attorney_count_confidence"] = site.attorney_count_confidence
        additional["attorney_count_is_min"] = site.attorney_count_is_min
    if site.years_in_operation is not None:
        additional["years_in_operation"] = site.years_in_operation

    rec: dict[str, Any] = {
        "source": "website",
        "source_firm_id": website,
        "source_url": source_url,
        "scraped_at": scraped_at,
        "raw_payload_path": raw_payload_path,
        "http_status": http_status,
        "name_raw": site.name_raw or "",
        "website_raw": source_url,
        "phone_raw": site.phones[0] if site.phones else None,
        "year_founded": site.year_founded,
        "attorney_count": site.attorney_count,
        "deactivation_status": site.deactivation_status,
        "primary_city": site.primary_city,
        "primary_state": site.primary_state,
        "primary_postal_code": site.primary_postal_code,
        "office_count": site.office_count,
        "firm_short_description": site.firm_short_description,
        "firm_descriptions": site.firm_descriptions or None,
        "contacts": [dict(c) for c in site.contacts],
        "offices": offices,
        "practice_areas_raw": practice_raw,
        "additional_data": additional,
    }
    # Fill name_normalized / website_normalized / phone_normalized / office
    # normalization / practice_areas_matched+unmatched (shared, source-uniform).
    return normalize_record(rec)


def _upsert_fsr(engine, records: list[dict[str, Any]]) -> dict[str, int]:
    """Insert/update source="website" FirmSourceRecord rows (full field set).

    One transaction per call (the caller chunks), wrapped in the same WAL
    lock-retry as scrape_az_bar.upsert_firm_source_records: a transient "database
    is locked" rolls back (fresh session) and RE-APPLIES the unchanged batch, so it
    coexists with the live Martindale writer.
    """
    if not records:
        return {"inserted": 0, "updated": 0}
    for attempt in range(6):
        try:
            inserted = updated = 0
            with Session(engine) as session:
                for r in records:
                    fields = {c: r.get(c) for c in _FSR_COLUMNS}
                    fields["name_raw"] = fields.get("name_raw") or ""
                    for jcol, default in (
                        ("contacts", []),
                        ("offices", []),
                        ("practice_areas_raw", []),
                        ("practice_areas_matched", []),
                        ("practice_areas_unmatched", []),
                        ("additional_data", {}),
                    ):
                        if fields.get(jcol) is None:
                            fields[jcol] = default
                    existing = session.scalar(
                        select(FirmSourceRecord).where(
                            FirmSourceRecord.source == r["source"],
                            FirmSourceRecord.source_firm_id == r["source_firm_id"],
                        )
                    )
                    if existing is None:
                        session.add(FirmSourceRecord(**fields))
                        inserted += 1
                    else:
                        for k, v in fields.items():
                            setattr(existing, k, v)
                        updated += 1
                session.commit()
            return {"inserted": inserted, "updated": updated}
        except OperationalError:
            if attempt == 5:
                raise
            time.sleep(0.5 * (2**attempt))
    return {"inserted": 0, "updated": 0}


def run_load_fsr(*, flush_every: int = 400, limit: int | None = None) -> None:
    """Re-extract cached raw (NO network) and upsert each real firm site as a
    source="website" FirmSourceRecord. Chunked single-committer (WAL + retry), safe
    to run concurrently with the Martindale scrape (rows disjoint by `source`;
    COORDINATION 2026-06-08 14:25). `limit` caps emitted rows for a pilot
    (`--limit 0` = full corpus).
    """
    configure_logging()
    settings = get_settings()
    base = settings.raw_data_dir / "firm_websites"
    if not base.exists():
        print(f"No firm_websites raw data at {base}", file=sys.stderr)
        return
    buckets: dict[str, dict[str, Path]] = {}
    for gz in sorted(base.glob("*/*/*.html.gz")):
        buckets.setdefault(gz.parent.name, {})[gz.stem.replace(".html", "")] = gz
    engine = make_engine()
    buffer: list[dict[str, Any]] = []
    emitted = skipped = seen = failed = 0
    for files in buckets.values():
        home = files.get("home")
        if home is None:
            continue
        seen += 1
        sc = _sidecar(home)
        cand = sc.get("url") or ""
        final = sc.get("final_url") or cand
        website = re.sub(r"^https?://(www\.)?", "", cand).split("/")[0].lower()
        if not website:
            continue
        try:
            pages: list[tuple[str, bytes]] = []
            for name, path in sorted(files.items()):
                role = "home" if name == "home" else re.sub(r"\d+$", "", name)
                pages.append((role, _read_gz(path)))
            site = extract_site(pages, base_url=cand)
            rec = fsr_record_from_site(
                website,
                site,
                source_url=final or f"https://{website}",
                http_status=sc.get("status"),
                raw_payload_path=str(home),
                scraped_at=datetime.fromtimestamp(home.stat().st_mtime, tz=UTC),
            )
        except Exception as exc:  # one bad firm must never abort the whole batch
            log.warning("enrich.fsr_extract_failed", website=website, error=str(exc))
            failed += 1
            continue
        if rec is None:
            skipped += 1
            continue
        buffer.append(rec)
        if len(buffer) >= flush_every:
            c = _upsert_fsr(engine, buffer)
            emitted += c["inserted"] + c["updated"]
            log.info("enrich.fsr_flush", emitted=emitted, skipped=skipped, failed=failed, seen=seen)
            buffer.clear()
        if limit and emitted + len(buffer) >= limit:
            break
    if buffer:
        c = _upsert_fsr(engine, buffer)
        emitted += c["inserted"] + c["updated"]
    print(
        f"fsr-load: upserted {emitted} website source rows "
        f"({skipped} non-firm skipped, {failed} extract-failed) from {seen} cached homes."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=("pilot", "run", "load", "load-fsr"))
    ap.add_argument("--websites", type=str, default=None, help="(pilot) comma list of bare domains")
    ap.add_argument("--limit", type=int, default=30, help="cap number of firms")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--flush-every", type=int, default=50)
    args = ap.parse_args()
    if args.mode == "pilot":
        sites = [w.strip().lower() for w in args.websites.split(",")] if args.websites else None
        run_pilot(websites=sites, limit=args.limit, workers=args.workers)
    elif args.mode == "load":
        run_load()
    elif args.mode == "load-fsr":
        run_load_fsr(limit=args.limit if args.limit and args.limit > 0 else None)
    else:
        run(
            limit=args.limit if args.limit and args.limit > 0 else None,
            workers=args.workers,
            flush_every=args.flush_every,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
