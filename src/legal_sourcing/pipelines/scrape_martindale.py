"""Martindale-Hubbell scrape pipeline (pilot).

Phases:
    1. City sweep — for each requested (state_slug, city_slug), fetch
       the attorney-results page (paginating until cards run out) and
       parse cards via MartindaleCityParser.
    2. Firm-profile sweep — collect unique firm_profile_url values,
       fetch each, extract firm-level fields via parse_firm_profile().
    3. Merge — splice firm-level fields (website, sponsored flag, etc.)
       back into the per-attorney records via firm_profile_url.
    4. Normalize + aggregate (same path as AZ Bar).
    5. Upsert into FirmSourceRecord with source="martindale".

CLI:
    uv run python -m legal_sourcing.pipelines.scrape_martindale pilot

"Pilot" defaults to a small mix of cities for now — see PILOT_CITIES
below. Each can be a small / medium / large city; the goal is to
exercise firm aggregation across at least one large-attorney population.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from legal_sourcing.config import get_settings
from legal_sourcing.models import FirmSourceRecord
from legal_sourcing.parsers.martindale import (
    MartindaleCityParser,
    parse_firm_profile,
)
from legal_sourcing.pipelines.scrape_az_bar import (
    aggregate_by_firm,
    normalize_record,
    upsert_firm_source_records,
)
from legal_sourcing.scrapers.base import ScrapeError
from legal_sourcing.scrapers.martindale import MartindaleScraper, is_path_allowed
from legal_sourcing.utils.logging import configure_logging, get_logger

log = get_logger(__name__)


# Default cohort for the pilot. Small + medium + large mix; one Arizona
# city for cross-source comparison with AZ Bar.
PILOT_CITIES: list[tuple[str, str]] = [
    # (state_slug, city_slug)
    ("alabama", "abbeville"),
    ("alabama", "birmingham"),
    ("arizona", "phoenix"),
]


def _read_gz_bytes(path: Path) -> bytes:
    with gzip.open(path, "rb") as f:
        return f.read()


def _has_next_page(html_bytes: bytes) -> bool:
    """Heuristic — empty results page has zero `card--attorney` blocks."""
    # Avoid full DOM parse for the cheap check; use a regex hit count.
    return b'class="card card--attorney' in html_bytes or b"class='card card--attorney" in html_bytes


def fetch_city_pages(
    scraper: MartindaleScraper, *, state_slug: str, city_slug: str
) -> list[Path]:
    """Walk ?page=1..N until a page has no cards. Returns the raw paths."""
    paths: list[Path] = []
    page = 1
    while True:
        url = scraper.city_url(city_slug=city_slug, state_slug=state_slug, page=page)
        if not is_path_allowed(url):
            log.warning("martindale.path_blocked", url=url)
            break
        path = scraper.fetch_one(
            url,
            method="GET",
            bucket=f"city/{state_slug}",
            filename=f"{city_slug}_p{page:02d}",
        )
        if path is None:
            break
        paths.append(path)
        body = _read_gz_bytes(path)
        if not _has_next_page(body):
            log.info(
                "martindale.city_end",
                state=state_slug,
                city=city_slug,
                pages=page,
            )
            break
        page += 1
        if page > 50:
            # Safety: don't infinite-loop on a malformed page.
            log.warning(
                "martindale.city_page_cap",
                state=state_slug,
                city=city_slug,
                cap=50,
            )
            break
    return paths


def fetch_firm_profiles(
    scraper: MartindaleScraper, firm_urls: Iterable[str]
) -> dict[str, Path]:
    """Concurrent firm-profile fetch. Returns {firm_url: raw_path}."""
    firm_urls = sorted(set(firm_urls))
    out: dict[str, Path] = {}
    if not firm_urls:
        return out

    log.info("martindale.firm_sweep_start", count=len(firm_urls))
    with ThreadPoolExecutor(max_workers=scraper._workers) as pool:
        futures: dict[Any, str] = {}
        for url in firm_urls:
            if not is_path_allowed(url):
                log.warning("martindale.firm_path_blocked", url=url)
                continue
            # Use the firm slug as the filename for stable upsert.
            m = re.search(r"/organization/([^/]+)/([^/]+)/?$", url)
            stem = (m.group(1) + "__" + m.group(2)) if m else None
            stem = stem or "firm"
            futures[
                pool.submit(
                    scraper.fetch_one,
                    url,
                    method="GET",
                    bucket="firm",
                    filename=stem,
                )
            ] = url
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                p = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.error("martindale.firm_fetch_failed", url=url, error=str(exc))
                continue
            if p is not None:
                out[url] = p
    log.info("martindale.firm_sweep_done", got=len(out))
    return out


def parse_city_records(
    paths: Iterable[Path],
) -> list[dict[str, Any]]:
    parser = MartindaleCityParser()
    records: list[dict[str, Any]] = []
    for p in paths:
        recs = parser.parse_file(p)
        records.extend(recs)
    return records


def merge_firm_profiles(
    records: list[dict[str, Any]],
    firm_profile_paths: dict[str, Path],
) -> None:
    """Splice firm-profile data (website URL, sponsored, masthead phone)
    into per-attorney records via firm_profile_url. Mutates records in
    place.
    """
    cache: dict[str, dict[str, Any]] = {}
    for url, path in firm_profile_paths.items():
        try:
            cache[url] = parse_firm_profile(_read_gz_bytes(path))
        except Exception as exc:  # noqa: BLE001
            log.warning("martindale.firm_parse_failed", url=url, error=str(exc))

    for r in records:
        firm_url = (r.get("additional_data") or {}).get("firm_profile_url")
        if not firm_url or firm_url not in cache:
            continue
        profile = cache[firm_url]
        if profile.get("firm_website_url") and not r.get("website_raw"):
            r["website_raw"] = profile["firm_website_url"]
        # Phones: prefer card phone (more reliable), but fall back to firm
        # masthead phone when the card had none.
        if profile.get("firm_phone") and not r.get("phone_raw"):
            r["phone_raw"] = profile["firm_phone"]
        # Sponsored flag and any other extras land in additional_data.
        ad = r.setdefault("additional_data", {})
        if "firm_website_is_sponsored" in profile:
            ad["firm_website_is_sponsored"] = profile["firm_website_is_sponsored"]


def run_pilot(*, cities: list[tuple[str, str]] | None = None) -> None:
    settings = get_settings()
    configure_logging()
    cities = cities or PILOT_CITIES

    log.info("martindale.pilot_start", cities=len(cities))

    with MartindaleScraper() as scraper:
        # Phase 1: city sweep
        city_paths: list[Path] = []
        for state_slug, city_slug in cities:
            try:
                city_paths.extend(
                    fetch_city_pages(scraper, state_slug=state_slug, city_slug=city_slug)
                )
            except ScrapeError as exc:
                log.error(
                    "martindale.city_fetch_failed",
                    state=state_slug,
                    city=city_slug,
                    error=str(exc),
                )

        # Parse cards from city pages.
        records = parse_city_records(city_paths)
        log.info("martindale.cards_parsed", count=len(records))

        # Phase 2: firm-profile sweep — dedup the URL set first.
        firm_urls = {
            (r.get("additional_data") or {}).get("firm_profile_url")
            for r in records
        }
        firm_urls.discard(None)
        firm_paths = fetch_firm_profiles(scraper, firm_urls)  # type: ignore[arg-type]

    # Phase 3: merge firm-profile fields into the records.
    merge_firm_profiles(records, firm_paths)

    # Phase 4: normalize + aggregate.
    for r in records:
        normalize_record(r)
    firm_records = aggregate_by_firm(records)
    log.info(
        "martindale.aggregate_done",
        attorneys=len(records),
        firms=len(firm_records),
    )

    # Tag every emitted record with source provenance. Prefer the
    # firm-profile URL as the canonical source_url when we have it;
    # otherwise fall back to whatever city page first surfaced this
    # firm. source_url is informational only — uniqueness is enforced
    # on (source, source_firm_id).
    for r in firm_records:
        r.setdefault("source", "martindale")
        r.setdefault("scraped_at", dt.datetime.now(dt.timezone.utc))
        ad = r.get("additional_data") or {}
        fpu = ad.get("firm_profile_url")
        if fpu:
            r["source_url"] = fpu
        r.setdefault("source_url", "")

    # Phase 5: upsert
    engine = create_engine(settings.db_url)
    with Session(engine) as session:
        counts = upsert_firm_source_records(session, firm_records)
    log.info("martindale.upsert_done", **counts)
    print(
        f"Martindale pilot complete: {len(records)} attorneys -> "
        f"{len(firm_records)} firms ({counts['inserted']} inserted, "
        f"{counts['updated']} updated)."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("pilot",))
    args = parser.parse_args()
    if args.mode == "pilot":
        run_pilot()
    return 0


if __name__ == "__main__":
    sys.exit(main())
