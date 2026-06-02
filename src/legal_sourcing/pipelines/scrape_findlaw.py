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
~5-10 minutes depending on how many practice-area-city combos have
data. Use `--max-pages-per-combo N` to cap pagination for a smaller
smoke test.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from selectolax.parser import HTMLParser
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from legal_sourcing.config import get_settings
from legal_sourcing.geo import parse_states_arg
from legal_sourcing.parsers.findlaw import (
    FindLawCityParser,
    extract_page_meta,
)
from legal_sourcing.pipelines._checkpoint import Checkpoint
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
    "personal-injury-plaintiff",  # Personal Injury
    "premises-liability-plaintiff",  # Premises Liability
    "medical-malpractice",
    "wrongful-death-plaintiff",
    "products-liability-law",  # Dangerous Products
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


# ---------------------------------------------------------------------------
# National scrape: per (practice_area, state) discover cities, then sweep
# each city across the practice areas that listed it, committing per city.


def extract_city_slugs(html: str, practice_area_slug: str, state_slug: str) -> list[str]:
    """Pull city slugs off a FindLaw practice-area/state index page.

    Links look like `/{pa}/{state}/{city}/`. We drop two kinds of
    non-city links that share the shape:

      * `all-cities` — a nav link back to the full listing.
      * `*-county` — county aggregation pages that overlap city pages;
        their firms would be re-discovered (and de-duped) at the city
        grain anyway, so skipping them avoids redundant fetches.
    """
    pat = re.compile(rf"/{re.escape(practice_area_slug)}/{re.escape(state_slug)}/([^/?#]+)/?$")
    tree = HTMLParser(html)
    out: set[str] = set()
    for a in tree.css("a"):
        href = a.attributes.get("href") or ""
        m = pat.search(href)
        if not m:
            continue
        city = m.group(1)
        if city == "all-cities" or city.endswith("-county"):
            continue
        out.add(city)
    return sorted(out)


def discover_state(
    scraper: FindLawScraper,
    state_slug: str,
    practice_areas: list[str],
) -> dict[str, list[str]]:
    """Build `{city_slug: [practice_area, ...]}` for one state.

    We fetch each (practice_area, state) index page and union the city
    lists, recording which practice areas actually listed each city.
    The city sweep then visits ONLY the (city, pa) combos that exist —
    no blind 9x fan-out into empty pages. State-index pages are stored
    under `state_index/{state}/` so `load` can rebuild this map offline.
    """
    city_pas: dict[str, set[str]] = defaultdict(set)
    for pa in practice_areas:
        url = scraper.practice_area_state_url(practice_area_slug=pa, state_slug=state_slug)
        try:
            path = scraper.fetch_one(
                url,
                method="GET",
                bucket=f"state_index/{state_slug}",
                filename=pa,
            )
        except ScrapeError as exc:
            log.warning(
                "findlaw.state_index_failed",
                practice_area=pa,
                state=state_slug,
                error=str(exc)[:200],
            )
            continue
        if path is None:
            continue
        html = _read_gz_bytes(path).decode("utf-8", errors="replace")
        for city in extract_city_slugs(html, pa, state_slug):
            city_pas[city].add(pa)
    return {city: sorted(pas) for city, pas in city_pas.items()}


def _tag_provenance(firm_records: list[dict[str, Any]]) -> None:
    now = dt.datetime.now(dt.UTC)
    for r in firm_records:
        r.setdefault("source", "findlaw")
        r.setdefault("scraped_at", now)
        ad = r.get("additional_data") or {}
        fpu = ad.get("firm_profile_url")
        if fpu:
            r["source_url"] = fpu
        r.setdefault("source_url", "")


def _process_city(
    state_slug: str,
    city_slug: str,
    records: list[dict[str, Any]],
    engine: Any,
) -> dict[str, int]:
    """Normalize -> aggregate -> upsert one city's accumulated cards.

    FindLaw cards for one firm recur across practice-area URLs; we
    aggregate the *whole city* at once so `practice_areas_raw` unions
    correctly before the upsert (which overwrites, not unions). This is
    the per-city durable unit committed in its own Session.
    """
    for r in records:
        normalize_record(r)
    firm_records = aggregate_by_firm(records)
    _tag_provenance(firm_records)
    with Session(engine) as session:
        counts = upsert_firm_source_records(session, firm_records)
    log.info(
        "findlaw.city_upserted",
        state=state_slug,
        city=city_slug,
        cards=len(records),
        firms=len(firm_records),
        **counts,
    )
    return counts


def run_full(
    *,
    practice_areas: list[str] | None = None,
    states: list[str] | None = None,
    max_pages_per_combo: int | None = None,
    resume: bool = True,
) -> None:
    """National FindLaw sweep, committed one city at a time.

    For each state we discover the city->practice-areas map, then for
    each city we sweep every practice area that listed it, accumulate
    the cards, aggregate, and upsert. Progress is checkpointed per city
    so the (multi-day) run resumes cleanly after any interruption.

    A Cloudflare challenge stops the run gracefully: the checkpoint is
    already current, so re-running later picks up where we left off. We
    do NOT try to defeat the challenge.
    """
    settings = get_settings()
    configure_logging()
    practice_areas = practice_areas or PILOT_PRACTICE_AREAS
    states = states if states is not None else parse_states_arg("all")
    cp = Checkpoint("findlaw_full")

    log.info(
        "findlaw.full_start",
        practice_areas=len(practice_areas),
        states=len(states),
        max_pages_per_combo=max_pages_per_combo,
        resume=resume,
        already_done=cp.completed_count,
    )

    engine = create_engine(settings.db_url, connect_args={"timeout": 30})
    try:
        with FindLawScraper() as scraper:
            for state_slug in states:
                city_pas = discover_state(scraper, state_slug, practice_areas)
                log.info(
                    "findlaw.state_discovered",
                    state=state_slug,
                    cities=len(city_pas),
                )
                for city_slug, pas in sorted(city_pas.items()):
                    key = f"{state_slug}/{city_slug}"
                    if resume and cp.is_done(key):
                        continue
                    records: list[dict[str, Any]] = []
                    for pa in pas:
                        paths = fetch_combo_pages(
                            scraper,
                            practice_area_slug=pa,
                            state_slug=state_slug,
                            city_slug=city_slug,
                            max_pages=max_pages_per_combo,
                        )
                        records.extend(parse_paths(paths))
                    counts = _process_city(state_slug, city_slug, records, engine)
                    cp.mark_done(
                        key,
                        inserted=counts["inserted"],
                        updated=counts["updated"],
                    )
    except FindLawCloudflareChallenge as exc:
        log.error("findlaw.cloudflare_abort", error=str(exc)[:300])
        print(
            f"STOPPED on Cloudflare challenge: {exc}\n"
            f"Checkpoint saved ({cp.completed_count} cities done). "
            f"Re-run the same command later to resume.",
            file=sys.stderr,
        )
        return

    t = cp.totals
    log.info("findlaw.full_done", **t)
    print(
        f"FindLaw full complete: {t['cities']} cities committed "
        f"({t['inserted']} inserted, {t['updated']} updated total)."
    )


def run_load(*, date_str: str | None = None, resume: bool = False) -> None:
    """Re-parse already-fetched FindLaw pages from disk and upsert per city.

    No network. Groups raw pages by (state, city) across all practice
    areas — layout is `city/{pa}/{state}/{city}_pNN.html.gz` — so the
    per-city practice-area union is reconstructed before the upsert,
    exactly as a live `full` run would.
    """
    settings = get_settings()
    configure_logging()
    base = settings.raw_data_dir / "findlaw"
    if not base.exists():
        print(f"No FindLaw raw data at {base}", file=sys.stderr)
        return
    if date_str is None:
        date_dirs = sorted([p for p in base.iterdir() if p.is_dir()])
        if not date_dirs:
            print(f"No date partitions under {base}", file=sys.stderr)
            return
        date_str = date_dirs[-1].name
    city_root = base / date_str / "city"
    if not city_root.exists():
        print(f"No city/ dir at {city_root}", file=sys.stderr)
        return

    # Layout: city/{pa}/{state}/{city}_p{NN}.html.gz -> group by (state, city)
    groups: dict[tuple[str, str], list[Path]] = {}
    for gz in city_root.glob("*/*/*.html.gz"):
        state_slug = gz.parent.name
        stem = gz.name.split(".", 1)[0]
        city_slug = stem.rsplit("_p", 1)[0]
        groups.setdefault((state_slug, city_slug), []).append(gz)

    log.info(
        "findlaw.load_start",
        date=date_str,
        cities=len(groups),
        resume=resume,
    )
    cp = Checkpoint("findlaw_load") if resume else None
    engine = create_engine(settings.db_url, connect_args={"timeout": 30})
    total = {"inserted": 0, "updated": 0, "cities": 0}
    for (state_slug, city_slug), paths in sorted(groups.items()):
        key = f"{state_slug}/{city_slug}"
        if cp is not None and cp.is_done(key):
            continue
        records = parse_paths(sorted(paths))
        counts = _process_city(state_slug, city_slug, records, engine)
        total["inserted"] += counts["inserted"]
        total["updated"] += counts["updated"]
        total["cities"] += 1
        if cp is not None:
            cp.mark_done(key, inserted=counts["inserted"], updated=counts["updated"])
    log.info("findlaw.load_done", **total)
    print(
        f"FindLaw load complete: {total['cities']} cities "
        f"({total['inserted']} inserted, {total['updated']} updated)."
    )


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
        r.setdefault("scraped_at", dt.datetime.now(dt.UTC))
        # source_url: prefer the firm-profile URL when we captured it.
        ad = r.get("additional_data") or {}
        fpu = ad.get("firm_profile_url")
        if fpu:
            r["source_url"] = fpu
        r.setdefault("source_url", "")

    engine = create_engine(settings.db_url, connect_args={"timeout": 30})
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
    parser.add_argument(
        "mode",
        choices=("pilot", "full", "load"),
        help="pilot = PI cluster over fixed cohort. full = national "
        "state->city sweep (resumable, per-city commit). load = "
        "re-parse fetched pages from disk (no network).",
    )
    parser.add_argument(
        "--max-pages-per-combo",
        type=int,
        default=5,
        help="Cap pagination per (practice-area, city) combo. Default 5. 0 = no cap.",
    )
    parser.add_argument(
        "--states",
        type=str,
        default="all",
        help="(full only) Comma-separated state slugs or 'all'. Default 'all'.",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="(load only) data/raw/findlaw/{date} partition to parse. Defaults to the most recent.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="(full only) Ignore the checkpoint and re-process every city.",
    )
    args = parser.parse_args()
    cap = args.max_pages_per_combo if args.max_pages_per_combo > 0 else None
    if args.mode == "pilot":
        run_pilot(max_pages_per_combo=cap)
    elif args.mode == "full":
        run_full(
            states=parse_states_arg(args.states),
            max_pages_per_combo=cap,
            resume=not args.no_resume,
        )
    elif args.mode == "load":
        run_load(date_str=args.date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
