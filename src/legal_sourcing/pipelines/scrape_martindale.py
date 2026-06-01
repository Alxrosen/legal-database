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
    extract_page_meta,
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


def fetch_city_pages(
    scraper: MartindaleScraper,
    *,
    state_slug: str,
    city_slug: str,
    max_pages: int | None = None,
) -> list[Path]:
    """Walk a city's listing pages using the documented pagination signals:

      * Page 1's `input.goToPage[data-max]` gives the deterministic total
        page count. We log it alongside `.results__total` and use it as
        the upper bound for the walk.
      * Each page's `a.arrow[rel="next"]` is checked as a per-iteration
        stop signal — defensive against the case where data-max can't
        be parsed.
      * At the end we compare scraped card count against `.results__total`
        and warn on big mismatches (the declared total is often inflated
        ~30%, but a 20-of-7000 outcome is a structural problem).

    `max_pages` caps the walk for pilot runs. None means walk to last_page.
    """
    paths: list[Path] = []
    page = 1
    declared_total: int | None = None
    last_page: int | None = None
    scraped_cards = 0
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

        body = _read_gz_bytes(path).decode("utf-8", errors="replace")
        meta = extract_page_meta(body)
        scraped_cards += meta["card_count"]

        if page == 1:
            declared_total = meta["results_total"]
            last_page = meta["last_page"]
            effective_target = last_page
            if max_pages is not None and last_page is not None:
                effective_target = min(last_page, max_pages)
            log.info(
                "martindale.city_meta",
                state=state_slug,
                city=city_slug,
                declared_total=declared_total,
                last_page=last_page,
                effective_target_pages=effective_target,
                max_pages_cap=max_pages,
            )

        # Stop conditions in priority order.
        if meta["card_count"] == 0:
            log.info(
                "martindale.city_end",
                state=state_slug,
                city=city_slug,
                reason="empty_page",
                pages=page,
            )
            break
        if max_pages is not None and page >= max_pages:
            log.info(
                "martindale.city_end",
                state=state_slug,
                city=city_slug,
                reason="max_pages_cap",
                pages=page,
            )
            break
        if last_page is not None and page >= last_page:
            log.info(
                "martindale.city_end",
                state=state_slug,
                city=city_slug,
                reason="last_page_reached",
                pages=page,
            )
            break
        if not meta["has_next"]:
            log.info(
                "martindale.city_end",
                state=state_slug,
                city=city_slug,
                reason="no_next_link",
                pages=page,
            )
            break

        page += 1
        if page > 500:  # safety net well beyond any realistic city
            log.warning(
                "martindale.city_page_cap",
                state=state_slug,
                city=city_slug,
                cap=500,
            )
            break

    # End-of-city sanity check on scraped count vs declared total.
    if (
        declared_total
        and scraped_cards > 0
        and (max_pages is None or last_page is None or max_pages >= last_page)
    ):
        # Only warn when we actually attempted the full walk.
        ratio = scraped_cards / declared_total
        if ratio < 0.5 or ratio > 1.5:
            log.warning(
                "martindale.count_mismatch",
                state=state_slug,
                city=city_slug,
                scraped_cards=scraped_cards,
                declared_total=declared_total,
                ratio=round(ratio, 3),
            )
    log.info(
        "martindale.city_done",
        state=state_slug,
        city=city_slug,
        pages_fetched=len(paths),
        cards_scraped=scraped_cards,
        declared_total=declared_total,
        last_page=last_page,
    )
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


def run_pilot(
    *,
    cities: list[tuple[str, str]] | None = None,
    max_pages_per_city: int | None = 3,
) -> None:
    settings = get_settings()
    configure_logging()
    cities = cities or PILOT_CITIES

    log.info(
        "martindale.pilot_start",
        cities=len(cities),
        max_pages_per_city=max_pages_per_city,
    )

    with MartindaleScraper() as scraper:
        # Phase 1: city sweep
        city_paths: list[Path] = []
        for state_slug, city_slug in cities:
            try:
                city_paths.extend(
                    fetch_city_pages(
                        scraper,
                        state_slug=state_slug,
                        city_slug=city_slug,
                        max_pages=max_pages_per_city,
                    )
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
    parser.add_argument(
        "--max-pages-per-city",
        type=int,
        default=3,
        help="Cap the per-city page walk. Default 3. Pass 0 to walk to last_page.",
    )
    args = parser.parse_args()
    cap = args.max_pages_per_city if args.max_pages_per_city > 0 else None
    if args.mode == "pilot":
        run_pilot(max_pages_per_city=cap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
