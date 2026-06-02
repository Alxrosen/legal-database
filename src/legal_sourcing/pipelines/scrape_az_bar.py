"""AZ Bar end-to-end scrape pipeline.

Phases:
    1. Reference  — fetch each small dropdown + ABS + Specializations
                    (informational; doesn't enter the DB yet).
    2. List       — paginate the search endpoint, collecting EntityNumbers.
                    Each list page is stored under data/raw/az_bar/{date}/list/.
    3. Detail     — concurrent detail fetch for each EntityNumber.
                    Each detail response stored under
                    data/raw/az_bar/{date}/detail/{EntityNumber}.json.gz.
    4. Parse      — read every detail .json.gz, run AZBarDetailParser.
    5. Normalize  — populate *_normalized fields and practice-area matches.
    6. Aggregate  — group attorneys by (firm_name_normalized,
                    primary_office_street_normalized) into firm rows.
    7. Upsert     — insert / update FirmSourceRecord rows in SQLite.

The principle "re-parsing never requires re-scraping" means phases 4-7
can be re-run against the saved raw data without re-hitting AZ Bar.

CLI:
    uv run python -m legal_sourcing.pipelines.scrape_az_bar pilot
    uv run python -m legal_sourcing.pipelines.scrape_az_bar full

`pilot` runs against the first PageSize=25 list page only — ~30 calls,
~15 distinct firms after aggregation, ideal for end-to-end smoke
testing without burdening AZ Bar.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from legal_sourcing.config import get_settings
from legal_sourcing.models import FirmSourceRecord
from legal_sourcing.normalize import (
    get_taxonomy,
    normalize_address,
    normalize_firm_name,
    normalize_phone,
    normalize_practice_area,
    normalize_url,
)
from legal_sourcing.parsers.az_bar import AZBarDetailParser
from legal_sourcing.scrapers.az_bar import AZBarScraper
from legal_sourcing.utils.logging import configure_logging, get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Scrape phases


def fetch_reference(scraper: AZBarScraper) -> None:
    """Fetch each reference dropdown + Specializations + ABS once per run."""
    endpoints = [
        ("specializations", scraper.SPECIALIZATIONS_PATH, False),
        ("states", scraper.STATES_PATH, False),
        ("counties", scraper.COUNTIES_PATH, False),
        ("jurisdictions", scraper.JURISDICTIONS_PATH, False),
        ("languages", scraper.LANGUAGES_PATH, False),
        ("law_schools", scraper.LAW_SCHOOLS_PATH, False),
        ("sections", scraper.SECTIONS_PATH, False),
        ("abs", scraper.ABS_PATH, True),
    ]
    for name, path, include_inactive in endpoints:
        url = scraper.reference_url(path, include_inactive=include_inactive)
        scraper.fetch_one(url, method="GET", bucket="reference", filename=name)
    log.info("pipeline.reference_done", count=len(endpoints))


def fetch_list_page(scraper: AZBarScraper, *, page: int, page_size: int) -> dict[str, Any]:
    """Fetch + parse one list page envelope. Returns the parsed JSON."""
    url = scraper.list_url(page=page, page_size=page_size, shuffle=False, seed="null")
    path = scraper.fetch_one(
        url,
        method="POST",
        json_body={},
        bucket="list",
        filename=f"page_{page:04d}_size{page_size}",
    )
    if path is None:
        raise RuntimeError(f"list fetch returned no path for page={page}")
    with gzip.open(path, "rb") as f:
        return json.loads(f.read())


def fetch_details(scraper: AZBarScraper, entity_numbers: Iterable[int]) -> list[Path]:
    """Concurrently fetch detail for each EntityNumber. Returns the
    list of gzipped raw payload paths written."""
    entity_numbers = list(entity_numbers)
    log.info("pipeline.detail_phase_start", count=len(entity_numbers))

    paths: list[Path] = []
    with ThreadPoolExecutor(max_workers=scraper._workers) as pool:
        futures = {
            pool.submit(
                scraper.fetch_one,
                scraper.detail_url(en),
                method="GET",
                bucket="detail",
                filename=str(en),
            ): en
            for en in entity_numbers
        }
        for fut in as_completed(futures):
            en = futures[fut]
            try:
                p = fut.result()
            except Exception as exc:
                log.error("pipeline.detail_failed", entity_number=en, error=str(exc))
                continue
            if p is not None:
                paths.append(p)

    log.info("pipeline.detail_phase_done", written=len(paths))
    return paths


# ---------------------------------------------------------------------------
# Parse + normalize + aggregate


def normalize_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Populate `*_normalized` fields and `practice_areas_matched` /
    `practice_areas_unmatched` on a parser-emitted record. Mutates in
    place and also returns the dict.
    """
    name_n = normalize_firm_name(rec.get("name_raw"))
    rec["name_normalized"] = name_n.normalized if name_n else None
    rec["website_normalized"] = normalize_url(rec.get("website_raw"))
    rec["phone_normalized"] = normalize_phone(rec.get("phone_raw"))

    # Contacts: lightweight per-attorney normalization.
    for c in rec.get("contacts", []):
        cn = normalize_firm_name(c.get("name_raw"))
        c["name_normalized"] = cn.normalized if cn else None
        c["phone_normalized"] = normalize_phone(c.get("phone_raw"))
        em = (c.get("email_raw") or "").strip().lower() or None
        c["email_normalized"] = em

    # Offices: when the source supplied structured raw fields we
    # preserve them directly. usaddress is reserved for the street
    # portion, where the input is genuinely free-text. Round-tripping
    # the whole address through usaddress mis-tagged "Dothan AL" as
    # `street="al dothan", city=None, state=None` because the tagger
    # is statistical and trips on missing commas / missing street
    # numbers.
    for o in rec.get("offices", []):
        street_components = " ".join(p for p in (o.get("street_raw"), o.get("street2_raw")) if p)
        norm: dict[str, Any] = {}
        if street_components:
            parsed_street = normalize_address(street_components)
            if parsed_street:
                norm["street"] = parsed_street.street_normalized
        # Title-case city so AZ Bar's all-caps "PHOENIX" doesn't end up
        # in a different bucket from Martindale's "Phoenix".
        city_raw = (o.get("city_raw") or "").strip()
        norm["city"] = city_raw.title() if city_raw else None
        state_raw = (o.get("state_raw") or "").replace(".", "").strip().upper()
        norm["state"] = state_raw[:2] if state_raw else None
        zip5 = (o.get("postal_code_raw") or "").strip().split("-")[0][:5]
        norm["postal_code"] = zip5 or None
        norm["country"] = (o.get("country_raw") or "US").upper()
        # Only attach if at least one non-default field was populated.
        if any(v and v != "US" for v in norm.values()):
            o["normalized"] = norm

    # Practice areas: match each raw string against the canonical taxonomy.
    raw_list = rec.get("practice_areas_raw") or []
    tax = get_taxonomy()
    matched: set[str] = set()
    unmatched: set[str] = set()
    for s in raw_list:
        slug = tax.match(s)
        if slug:
            matched.add(slug)
        else:
            n = normalize_practice_area(s)
            if n:
                unmatched.add(n)
    rec["practice_areas_matched"] = sorted(matched)
    rec["practice_areas_unmatched"] = sorted(unmatched)
    return rec


def _firm_id(
    name_normalized: str | None,
    street_normalized: str | None,
    *,
    fallback_entity_number: int | str | None = None,
) -> str:
    """Stable source_firm_id derived from (normalized firm name + street),
    OR for unaffiliated attorneys (no firm name on the AZ Bar record)
    derived from their EntityNumber so they don't all collapse into a
    single "<no name>" row.
    """
    if name_normalized:
        key = f"{name_normalized}|{street_normalized or '<noaddr>'}"
    else:
        # Solo / unaffiliated attorney — use the bar-side identity so
        # the row is stable across re-scrapes.
        key = f"__solo__|en={fallback_entity_number or '<unknown>'}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def aggregate_by_firm(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group per-attorney records by (firm_name_normalized,
    primary_office_street_normalized) and merge into one firm record
    per group.
    """
    groups: dict[tuple[str | None, str | None], dict[str, Any]] = {}

    for r in records:
        offices = r.get("offices") or []
        primary = offices[0] if offices else {}
        street_n = (primary.get("normalized") or {}).get("street")
        name_n = r.get("name_normalized")
        if name_n:
            # Firm-grain key: same name + same street -> same firm.
            key: tuple[Any, Any] = (name_n, street_n)
        else:
            # Unaffiliated attorney: do NOT collapse with other
            # unaffiliated attorneys. Key on the first contact's
            # EntityNumber so each solo attorney is their own row.
            contacts = r.get("contacts") or []
            en = contacts[0].get("entity_number") if contacts else None
            key = ("__solo__", en if en is not None else id(r))

        if key not in groups:
            # First time we've seen this firm/office — seed with this record.
            seed: dict[str, Any] = {
                k: v for k, v in r.items() if k != "contacts" and k != "offices"
            }
            seed["contacts"] = list(r.get("contacts", []))
            seed["offices"] = list(r.get("offices", []))
            # Preserve any source-supplied deactivation marker.
            seed.setdefault("deactivation_status", r.get("deactivation_status"))
            seed["practice_areas_raw"] = list(r.get("practice_areas_raw") or [])
            seed["practice_areas_matched"] = list(r.get("practice_areas_matched") or [])
            seed["practice_areas_unmatched"] = list(r.get("practice_areas_unmatched") or [])
            seed["additional_data"] = dict(r.get("additional_data") or {})
            groups[key] = seed
            continue

        agg = groups[key]
        agg["contacts"].extend(r.get("contacts", []))
        # Dedupe offices by normalized street.
        seen_streets = {(o.get("normalized") or {}).get("street") for o in agg["offices"]}
        for o in r.get("offices", []):
            s = (o.get("normalized") or {}).get("street")
            if s not in seen_streets:
                agg["offices"].append(o)
                seen_streets.add(s)
        # Union practice areas.
        agg["practice_areas_raw"] = sorted(
            set(agg["practice_areas_raw"]) | set(r.get("practice_areas_raw") or [])
        )
        agg["practice_areas_matched"] = sorted(
            set(agg["practice_areas_matched"]) | set(r.get("practice_areas_matched") or [])
        )
        agg["practice_areas_unmatched"] = sorted(
            set(agg["practice_areas_unmatched"]) | set(r.get("practice_areas_unmatched") or [])
        )
        # First non-None wins for firm-level fields.
        for fld in ("website_raw", "website_normalized", "phone_raw", "phone_normalized"):
            if not agg.get(fld):
                agg[fld] = r.get(fld)
        # additional_data: existing key wins, new keys union in.
        for k, v in (r.get("additional_data") or {}).items():
            agg["additional_data"].setdefault(k, v)

    # Finalize: compute attorney_count and source_firm_id per group.
    result: list[dict[str, Any]] = []
    for key, rec in groups.items():
        rec["attorney_count"] = len(rec["contacts"])
        if key[0] == "__solo__":
            rec["source_firm_id"] = _firm_id(None, None, fallback_entity_number=key[1])
        else:
            name_n, street_n = key
            rec["source_firm_id"] = _firm_id(name_n, street_n)
        result.append(rec)
    return result


def parse_detail_payloads(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Parse every detail .json.gz under `paths` (or its parent dirs)
    using AZBarDetailParser. Returns the flat list of per-attorney
    firm-shaped dicts ready for normalization + aggregation.
    """
    parser = AZBarDetailParser()
    records: list[dict[str, Any]] = []
    for p in paths:
        recs = parser.parse_file(p)
        records.extend(recs)
    return records


# ---------------------------------------------------------------------------
# DB upsert


def upsert_firm_source_records(session: Session, records: list[dict[str, Any]]) -> dict[str, int]:
    """Insert or update FirmSourceRecord rows. Returns counts."""
    inserted = 0
    updated = 0

    for r in records:
        scraped_at = r.get("scraped_at")
        if isinstance(scraped_at, str):
            scraped_at = dt.datetime.fromisoformat(scraped_at)

        existing = session.scalar(
            select(FirmSourceRecord).where(
                FirmSourceRecord.source == r["source"],
                FirmSourceRecord.source_firm_id == r["source_firm_id"],
            )
        )

        common_fields = dict(
            source=r["source"],
            source_firm_id=r["source_firm_id"],
            source_url=r["source_url"],
            scraped_at=scraped_at,
            raw_payload_path=r.get("raw_payload_path"),
            http_status=r.get("http_status"),
            name_raw=r.get("name_raw") or "",
            name_normalized=r.get("name_normalized"),
            website_raw=r.get("website_raw"),
            website_normalized=r.get("website_normalized"),
            phone_raw=r.get("phone_raw"),
            phone_normalized=r.get("phone_normalized"),
            year_founded=r.get("year_founded"),
            attorney_count=r.get("attorney_count"),
            source_last_updated_at=r.get("source_last_updated_at"),
            deactivation_status=r.get("deactivation_status"),
            contacts=r.get("contacts", []),
            offices=r.get("offices", []),
            practice_areas_raw=r.get("practice_areas_raw", []),
            practice_areas_matched=r.get("practice_areas_matched", []),
            practice_areas_unmatched=r.get("practice_areas_unmatched", []),
            additional_data=r.get("additional_data", {}),
        )

        if existing is None:
            session.add(FirmSourceRecord(**common_fields))
            inserted += 1
        else:
            for k, v in common_fields.items():
                setattr(existing, k, v)
            updated += 1

    session.commit()
    return {"inserted": inserted, "updated": updated}


# ---------------------------------------------------------------------------
# Orchestrators


def run_pilot(
    *,
    page: int = 1,
    page_size: int = 25,
    num_pages: int = 1,
) -> None:
    """Pilot: fetch `num_pages` consecutive list pages starting at
    `page` (PageSize=`page_size`) and the detail of every attorney
    they surface, then parse / aggregate / upsert.

    Defaults reproduce the original 1-page-of-25 pilot. Bump
    `num_pages` and/or `page_size` to sweep a larger slice of the
    directory.
    """
    settings = get_settings()
    configure_logging()

    with AZBarScraper() as scraper:
        log.info(
            "pipeline.start",
            mode="pilot",
            page=page,
            page_size=page_size,
            num_pages=num_pages,
        )

        # Reference phase — small and informational.
        fetch_reference(scraper)

        # Multi-page list sweep: walk `num_pages` consecutive list pages
        # accumulating EntityNumbers. Stop early on empty Results.
        entity_numbers: list[int] = []
        for offset in range(num_pages):
            p = page + offset
            envelope = fetch_list_page(scraper, page=p, page_size=page_size)
            attorneys = (envelope.get("Result") or {}).get("Results") or []
            ens = [a["EntityNumber"] for a in attorneys if a.get("EntityNumber")]
            entity_numbers.extend(ens)
            log.info(
                "pipeline.list_page",
                page=p,
                got=len(ens),
                cumulative=len(entity_numbers),
            )
            if not ens:
                log.info("pipeline.list_short_or_empty", page=p)
                break
        log.info("pipeline.list_done", entity_count=len(entity_numbers))

        # Detail phase — concurrent.
        detail_paths = fetch_details(scraper, entity_numbers)

    # Parse + normalize + aggregate (no scraper needed beyond this point).
    raw_records = parse_detail_payloads(detail_paths)
    for r in raw_records:
        normalize_record(r)
    firm_records = aggregate_by_firm(raw_records)

    log.info(
        "pipeline.aggregate_done",
        attorneys=len(raw_records),
        firms=len(firm_records),
    )

    # Upsert.
    engine = create_engine(settings.db_url)
    with Session(engine) as session:
        counts = upsert_firm_source_records(session, firm_records)
    log.info("pipeline.upsert_done", **counts)
    print(
        f"Pilot complete: {len(raw_records)} attorneys -> "
        f"{len(firm_records)} firms ({counts['inserted']} inserted, "
        f"{counts['updated']} updated)."
    )


def run_full() -> None:
    """Full scrape — paginate the entire directory."""
    settings = get_settings()
    configure_logging()
    PAGE_SIZE = 200

    with AZBarScraper() as scraper:
        log.info("pipeline.start", mode="full", page_size=PAGE_SIZE)
        fetch_reference(scraper)

        # Paginate.
        all_entity_numbers: list[int] = []
        page = 1
        while True:
            envelope = fetch_list_page(scraper, page=page, page_size=PAGE_SIZE)
            results = (envelope.get("Result") or {}).get("Results") or []
            if not results:
                log.info("pipeline.list_end_of_results", page=page)
                break
            all_entity_numbers.extend(a["EntityNumber"] for a in results if a.get("EntityNumber"))
            if len(results) < PAGE_SIZE:
                log.info(
                    "pipeline.list_short_page",
                    page=page,
                    got=len(results),
                    expected=PAGE_SIZE,
                )
                break
            page += 1

        log.info("pipeline.list_done", entity_count=len(all_entity_numbers))
        detail_paths = fetch_details(scraper, all_entity_numbers)

    raw_records = parse_detail_payloads(detail_paths)
    for r in raw_records:
        normalize_record(r)
    firm_records = aggregate_by_firm(raw_records)
    log.info(
        "pipeline.aggregate_done",
        attorneys=len(raw_records),
        firms=len(firm_records),
    )

    engine = create_engine(settings.db_url)
    with Session(engine) as session:
        counts = upsert_firm_source_records(session, firm_records)
    log.info("pipeline.upsert_done", **counts)
    print(
        f"Full scrape complete: {len(raw_records)} attorneys -> "
        f"{len(firm_records)} firms ({counts['inserted']} inserted, "
        f"{counts['updated']} updated)."
    )


def _process_and_upsert(detail_paths: list[Path], *, mode: str) -> None:
    """Shared tail: parse -> normalize -> aggregate -> upsert.

    Used by both `full` (after fetching) and `load` (from disk). This
    is the part that crashed mid-run before the urlparse fix — keeping
    it in one place means a future crash here is recoverable by just
    re-running `load` against the already-fetched raw files.
    """
    settings = get_settings()
    raw_records = parse_detail_payloads(detail_paths)
    for r in raw_records:
        normalize_record(r)
    firm_records = aggregate_by_firm(raw_records)
    log.info(
        "pipeline.aggregate_done",
        mode=mode,
        attorneys=len(raw_records),
        firms=len(firm_records),
    )
    engine = create_engine(settings.db_url)
    with Session(engine) as session:
        counts = upsert_firm_source_records(session, firm_records)
    log.info("pipeline.upsert_done", **counts)
    print(
        f"{mode}: {len(raw_records)} attorneys -> {len(firm_records)} firms "
        f"({counts['inserted']} inserted, {counts['updated']} updated)."
    )


def run_load(*, date_str: str | None = None) -> None:
    """Parse + upsert from already-fetched raw detail files on disk.

    No network. Honors the project principle that re-parsing must
    never require re-scraping. If a `full` run crashes in the
    normalize/aggregate phase (as happened on Py 3.14 with a malformed
    URL), the fetched files are intact — `load` recovers them.

    `date_str` selects the data/raw/az_bar/{date}/detail partition;
    defaults to the most recent date present.
    """
    settings = get_settings()
    configure_logging()
    base = settings.raw_data_dir / "az_bar"
    if not base.exists():
        print(f"No AZ Bar raw data at {base}", file=sys.stderr)
        return
    if date_str is None:
        date_dirs = sorted([p for p in base.iterdir() if p.is_dir()])
        if not date_dirs:
            print(f"No date partitions under {base}", file=sys.stderr)
            return
        date_str = date_dirs[-1].name
    detail_dir = base / date_str / "detail"
    if not detail_dir.exists():
        print(f"No detail/ dir at {detail_dir}", file=sys.stderr)
        return
    detail_paths = sorted(detail_dir.glob("*.json.gz"))
    log.info(
        "pipeline.load_start",
        date=date_str,
        detail_files=len(detail_paths),
    )
    _process_and_upsert(detail_paths, mode="load")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("pilot", "full", "load"),
        help="pilot=one page; full=whole directory; "
        "load=parse already-fetched raw files (no network).",
    )
    parser.add_argument(
        "--page",
        type=int,
        default=1,
        help="List page to pilot from (default 1). Higher values sample "
        "further into the alphabetical directory.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=25,
        help="PageSize for the pilot list call (default 25).",
    )
    parser.add_argument(
        "--num-pages",
        type=int,
        default=1,
        help="How many consecutive list pages to walk from --page (default 1).",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="(load only) data/raw/az_bar/{date} partition to parse. Defaults to the most recent.",
    )
    args = parser.parse_args()
    if args.mode == "pilot":
        run_pilot(
            page=args.page,
            page_size=args.page_size,
            num_pages=args.num_pages,
        )
    elif args.mode == "load":
        run_load(date_str=args.date)
    else:
        run_full()
    return 0


if __name__ == "__main__":
    sys.exit(main())
