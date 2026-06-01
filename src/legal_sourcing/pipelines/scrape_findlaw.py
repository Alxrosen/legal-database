"""FindLaw scrape pilot pipeline.

Cross-product of (practice_area_slug, city) -> SRP pages. Each page
yields firm-shaped records (FindLaw cards are firm-level, no
individual attorneys). The pipeline aggregates by name + street
across practice-area contexts so a firm that shows up under e.g.
"motor-vehicle-accidents-plaintiff" AND "personal-injury-plaintiff"
collapses to one row whose `practice_areas_raw` lists both slugs.

CLI::

    uv run python -m legal_sourcing.pipelines.scrape_findlaw pilot

`pilot` walks the PI-priority cluster (PILOT_PRACTICE_AREAS) across
the Martindale cohort cities (PILOT_CITIES). At 0.33 RPS this is
~5–10 minutes depending on how many practice-area-city combos have
data. Use `--max-pages-per-combo N` to cap pagination for a smaller
smoke test.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from legal_sourcing.config import get_settings
from legal_sourcing.parsers.findlaw import (
    FindLawCityParser,
    extract_page_meta,
)
from legal_sourcing.pipelines.scrape_az_bar import (
    aggregate_by_firm,
    normalize_record,
    upsert_firm_source_records,
)
from legal_sourcing.scrapers.base import ScrapeError
from legal_sourcing.scrapers.findlaw import (
    FindLawCloudflareChallenge,
    FindLawScraper,
)
from legal_sourcing.utils.logging import configure_logging, get_logger

log = get_logger(__name__)


# Personal-injury cluster — FindLaw slug -> human label. Aligns with
# the project's priority taxonomy in data/reference/practice_areas.yaml.
PILOT_PRACTICE_AREAS: list[str] = [
    "motor-vehicle-accidents-plaintiff",  # Car Accidents
    "personal-injury-plaintiff",          # Personal Injury
    "premises-liability-plaintiff",       # Premises Liability
    "medical-malpractice",
    "wrongful-death-plaintiff",
    "products-liability-law",             # Dangerous Products
    "workers-compensation-law",
    "truck-accident",
    "birth-injury",
]

# Mirror the Martindale cohort so the M6 resolution pass has real
# cross-source matches to work on.
PILOT_CITIES: list[tuple[str, str]] = [
    ("alabama", "abbeville"),
    ("alabama", "birmingham"),
    ("alabama", "mobile"),
    ("arizona", "phoenix"),
    ("arizona", "tucson"),
]


def _read_gz_bytes(path: Path) -> bytes:
    with gzip.open(path, "rb") as f:
        return f.read()


def fetch_combo_pages(
    scraper: FindLawScraper,
    *,
    practice_area_slug: str,
    state_slug: str,
    city_slug: str,
    max_pages: int | None = 5,
) -> list[Path]:
    """Walk one (practice_area, state, city) listing using pagination
    metadata. Returns the gzipped raw paths fetched (empty list when
    the combo has no cards or 404s).
    """
    paths: list[Path] = []
    page = 1
    last_page: int | None = None
    results_total: int | None = None
    scraped_cards = 0
    while True:
        url = scraper.practice_area_city_url(
            practice_area_slug=practice_area_slug,
            state_slug=state_slug,
            city_slug=city_slug,
            page=page,
        )
        try:
            path = scraper.fetch_one(
                url,
                method="GET",
                bucket=f"city/{practice_area_slug}/{state_slug}",
                filename=f"{city_slug}_p{page:02d}",
            )
        except ScrapeError as exc:
            log.warning(
                "findlaw.combo_fetch_failed",
                practice_area=practice_area_slug,
                state=state_slug,
                city=city_slug,
                page=page,
                error=str(exc)[:200],
            )
            break
        if path is None:
            break

        body = _read_gz_bytes(path).decode("utf-8", errors="replace")
        meta = extract_page_meta(body)

        if page == 1:
            last_page = meta["last_page"]
            results_total = meta["results_total"]
            log.info(
                "findlaw.combo_meta",
                practice_area=practice_area_slug,
                state=state_slug,
                city=city_slug,
                last_page=last_page,
                results_total=results_total,
                card_count_p1=meta["card_count"],
            )
            # If no cards at all on page 1, the combo has nothing for us.
            if meta["card_count"] == 0:
                log.info(
                    "findlaw.combo_empty",
                    practice_area=practice_area_slug,
                    state=state_slug,
                    city=city_slug,
                )
                # Still keep the page-1 path so re-parses have it on disk.
                paths.append(path)
                return paths

        paths.append(path)
        scraped_cards += meta["card_count"]

        # Stop conditions.
        if meta["card_count"] == 0:
            break
        if max_pages is not None and page >= max_pages:
            break
        if last_page is not None and page >= last_page:
            break
        if not meta["has_next"]:
            break
        page += 1
        if page > 200:  # hard safety net
            log.warning(
                "findlaw.combo_page_cap",
                practice_area=practice_area_slug,
                state=state_slug,
                city=city_slug,
                cap=200,
            )
            break

    log.info(
        "findlaw.combo_done",
        practice_area=practice_area_slug,
        state=state_slug,
        city=city_slug,
        pages=len(paths),
        cards=scraped_cards,
    )
    return paths


def parse_paths(paths: list[Path]) -> list[dict[str, Any]]:
    parser = FindLawCityParser()
    records: list[dict[str, Any]] = []
    for p in paths:
        records.extend(parser.parse_file(p))
    return records


def run_pilot(
    *,
    practice_areas: list[str] | None = None,
    cities: list[tuple[str, str]] | None = None,
    max_pages_per_combo: int | None = 5,
) -> None:
    settings = get_settings()
    configure_logging()
    practice_areas = practice_areas or PILOT_PRACTICE_AREAS
    cities = cities or PILOT_CITIES

    log.info(
        "findlaw.pilot_start",
        practice_areas=len(practice_areas),
        cities=len(cities),
        combos=len(practice_areas) * len(cities),
        max_pages_per_combo=max_pages_per_combo,
    )

    paths: list[Path] = []
    try:
        with FindLawScraper() as scraper:
            for pa in practice_areas:
                for state_slug, city_slug in cities:
                    paths.extend(
                        fetch_combo_pages(
                            scraper,
                            practice_area_slug=pa,
                            state_slug=state_slug,
                            city_slug=city_slug,
                            max_pages=max_pages_per_combo,
                        )
                    )
    except FindLawCloudflareChallenge as exc:
        log.error("findlaw.cloudflare_abort", error=str(exc)[:300])
        print(f"FATAL: {exc}", file=sys.stderr)
        return

    records = parse_paths(paths)
    log.info("findlaw.cards_parsed", count=len(records))

    for r in records:
        normalize_record(r)
    firm_records = aggregate_by_firm(records)
    log.info(
        "findlaw.aggregate_done",
        cards=len(records),
        firms=len(firm_records),
    )

    # Tag provenance.
    for r in firm_records:
        r.setdefault("source", "findlaw")
        r.setdefault("scraped_at", dt.datetime.now(dt.timezone.utc))
        # source_url: prefer the firm-profile URL when we captured it.
        ad = r.get("additional_data") or {}
        fpu = ad.get("firm_profile_url")
        if fpu:
            r["source_url"] = fpu
        r.setdefault("source_url", "")

    engine = create_engine(settings.db_url)
    with Session(engine) as session:
        counts = upsert_firm_source_records(session, firm_records)
    log.info("findlaw.upsert_done", **counts)
    print(
        f"FindLaw pilot complete: {len(records)} cards -> "
        f"{len(firm_records)} firms ({counts['inserted']} inserted, "
        f"{counts['updated']} updated)."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("pilot",))
    parser.add_argument(
        "--max-pages-per-combo",
        type=int,
        default=5,
        help="Cap pagination per (practice-area, city) combo. Default 5. 0 = no cap.",
    )
    args = parser.parse_args()
    cap = args.max_pages_per_combo if args.max_pages_per_combo > 0 else None
    if args.mode == "pilot":
        run_pilot(max_pages_per_combo=cap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
