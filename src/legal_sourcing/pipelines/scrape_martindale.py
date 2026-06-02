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
import re
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from selectolax.parser import HTMLParser
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from legal_sourcing.config import get_settings
from legal_sourcing.geo import STATE_SLUG_TO_ABBR, parse_states_arg
from legal_sourcing.models import FirmSourceRecord
from legal_sourcing.parsers.martindale import (
    MartindaleCityParser,
    extract_page_meta,
    parse_firm_profile,
    parse_firm_profile_full,
)
from legal_sourcing.pipelines._checkpoint import Checkpoint
from legal_sourcing.pipelines.scrape_az_bar import (
    aggregate_by_firm,
    normalize_record,
    upsert_firm_source_records,
)
from legal_sourcing.scrapers.base import ScrapeError
from legal_sourcing.scrapers.martindale import MartindaleScraper, is_path_allowed
from legal_sourcing.utils.logging import configure_logging, get_logger

log = get_logger(__name__)


# Default cohort for the pilot. Small + medium + large mix; the two
# Arizona cities give us cross-source overlap with AZ Bar for the
# eventual M6 resolution pass.
PILOT_CITIES: list[tuple[str, str]] = [
    # (state_slug, city_slug)
    ("alabama", "abbeville"),  # tiny — confirms single-page path
    ("alabama", "birmingham"),  # big city, AL
    ("alabama", "mobile"),  # medium AL city
    ("arizona", "phoenix"),  # big city, AZ — overlap with AZ Bar
    ("arizona", "tucson"),  # medium AZ city — overlap with AZ Bar
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


def fetch_firm_profiles(scraper: MartindaleScraper, firm_urls: Iterable[str]) -> dict[str, Path]:
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
            except Exception as exc:
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
        except Exception as exc:
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


# ---------------------------------------------------------------------------
# National scrape: state -> city discovery, then per-city incremental upsert.


def extract_city_slugs(html: str, state_slug: str) -> list[str]:
    """Pull every city slug off a Martindale state index page.

    The state page (`/by-location/{state}-lawyers/`) links to each city
    as `/all-lawyers/{city}/{state}/`. We match that exact shape so we
    don't pick up unrelated nav links. Returns sorted, de-duplicated
    slugs.
    """
    pat = re.compile(rf"/all-lawyers/([^/]+)/{re.escape(state_slug)}/?$")
    tree = HTMLParser(html)
    out: set[str] = set()
    for a in tree.css("a"):
        href = a.attributes.get("href") or ""
        m = pat.search(href)
        if m:
            out.add(m.group(1))
    return sorted(out)


def discover_state_cities(scraper: MartindaleScraper, state_slug: str) -> list[str]:
    """Fetch a state index page and return its discovered city slugs.

    The page is stored under `state_index/` so a `load` run can rebuild
    the city universe from disk without re-fetching.
    """
    url = scraper.state_url(state_slug)
    if not is_path_allowed(url):
        log.warning("martindale.state_path_blocked", url=url)
        return []
    path = scraper.fetch_one(url, method="GET", bucket="state_index", filename=state_slug)
    if path is None:
        return []
    html = _read_gz_bytes(path).decode("utf-8", errors="replace")
    return extract_city_slugs(html, state_slug)


def _tag_provenance(firm_records: list[dict[str, Any]]) -> None:
    """Stamp source / scraped_at / source_url on aggregated firm dicts."""
    now = dt.datetime.now(dt.UTC)
    for r in firm_records:
        r.setdefault("source", "martindale")
        r.setdefault("scraped_at", now)
        ad = r.get("additional_data") or {}
        fpu = ad.get("firm_profile_url")
        if fpu:
            r["source_url"] = fpu
        r.setdefault("source_url", "")


def _backfill_office_state(records: list[dict[str, Any]], state_slug: str) -> None:
    """Backfill office ``state_raw`` from the swept URL's state.

    Martindale SRP cards show only the city; the state is implied by the
    ``/all-lawyers/{city}/{state}/`` URL. Without this, ~94% of Martindale
    firms have no state, which breaks ``primary_state``, the name_state
    blocking key, and state scoring downstream. Mutates in place.
    """
    abbr = STATE_SLUG_TO_ABBR.get(state_slug)
    if not abbr:
        return
    for r in records:
        for o in r.get("offices") or []:
            if not (o.get("state_raw") or "").strip():
                o["state_raw"] = abbr


def _process_city(
    state_slug: str,
    city_slug: str,
    paths: list[Path],
    engine: Any,
) -> dict[str, int]:
    """Parse -> normalize -> aggregate -> upsert one city's pages.

    Returns the upsert counts. This is the per-city durable unit: it
    commits inside its own Session so a crash on the *next* city can't
    roll this one back.
    """
    records = parse_city_records(paths)
    _backfill_office_state(records, state_slug)
    for r in records:
        normalize_record(r)
    firm_records = aggregate_by_firm(records)
    _tag_provenance(firm_records)
    with Session(engine) as session:
        counts = upsert_firm_source_records(session, firm_records)
    log.info(
        "martindale.city_upserted",
        state=state_slug,
        city=city_slug,
        attorneys=len(records),
        firms=len(firm_records),
        **counts,
    )
    return counts


def run_full(
    *,
    states: list[str] | None = None,
    max_pages_per_city: int | None = None,
    resume: bool = True,
) -> None:
    """National city sweep, committed one city at a time.

    For each state we discover its city slugs from the state index page,
    then for each city we sweep listing pages, parse, aggregate, and
    upsert immediately. Progress is checkpointed per city so the run is
    fully resumable across crashes / reboots — re-running with the same
    command skips cities already committed.

    NOTE: this pass does NOT fetch firm-profile pages (website fallback,
    descriptions, year founded). That is the job of the separate,
    already-resumable `enrich` mode, which would otherwise add one fetch
    per unique firm and balloon a national run by hundreds of thousands
    of requests. `full` gets the firm roster + card-level fields; run
    `enrich` afterwards to fill the rich fields.
    """
    settings = get_settings()
    configure_logging()
    states = states if states is not None else parse_states_arg("all")
    cp = Checkpoint("martindale_full")

    log.info(
        "martindale.full_start",
        states=len(states),
        max_pages_per_city=max_pages_per_city,
        resume=resume,
        already_done=cp.completed_count,
    )

    engine = create_engine(settings.db_url, connect_args={"timeout": 30})
    with MartindaleScraper() as scraper:
        for state_slug in states:
            try:
                cities = discover_state_cities(scraper, state_slug)
            except ScrapeError as exc:
                log.error(
                    "martindale.state_discovery_failed",
                    state=state_slug,
                    error=str(exc)[:300],
                )
                continue
            log.info(
                "martindale.state_discovered",
                state=state_slug,
                cities=len(cities),
            )
            for city_slug in cities:
                key = f"{state_slug}/{city_slug}"
                if resume and cp.is_done(key):
                    continue
                try:
                    paths = fetch_city_pages(
                        scraper,
                        state_slug=state_slug,
                        city_slug=city_slug,
                        max_pages=max_pages_per_city,
                    )
                    counts = _process_city(state_slug, city_slug, paths, engine)
                except ScrapeError as exc:
                    log.error(
                        "martindale.city_failed",
                        state=state_slug,
                        city=city_slug,
                        error=str(exc)[:300],
                    )
                    continue
                cp.mark_done(key, inserted=counts["inserted"], updated=counts["updated"])

    t = cp.totals
    log.info("martindale.full_done", **t)
    print(
        f"Martindale full complete: {t['cities']} cities committed "
        f"({t['inserted']} inserted, {t['updated']} updated total). "
        f"Run `enrich` next to fetch firm profiles."
    )


def run_load(*, date_str: str | None = None, resume: bool = False) -> None:
    """Re-parse already-fetched city pages from disk and upsert per city.

    No network. Honors "re-parsing never requires re-scraping": if a
    `full` run is interrupted mid-normalize, the fetched HTML is intact
    and `load` rebuilds the DB rows from it. Groups files by
    (state, city) from the `city/{state}/{city}_pNN.html.gz` layout.
    """
    settings = get_settings()
    configure_logging()
    base = settings.raw_data_dir / "martindale"
    if not base.exists():
        print(f"No Martindale raw data at {base}", file=sys.stderr)
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

    # Group the gz pages by (state, city): layout is
    # city/{state}/{city}_p{NN}.html.gz
    groups: dict[tuple[str, str], list[Path]] = {}
    for gz in city_root.glob("*/*.html.gz"):
        state_slug = gz.parent.name
        stem = gz.name.split(".", 1)[0]  # drop .html.gz
        city_slug = stem.rsplit("_p", 1)[0]
        groups.setdefault((state_slug, city_slug), []).append(gz)

    log.info(
        "martindale.load_start",
        date=date_str,
        cities=len(groups),
        resume=resume,
    )
    cp = Checkpoint("martindale_load") if resume else None
    engine = create_engine(settings.db_url, connect_args={"timeout": 30})
    total = {"inserted": 0, "updated": 0, "cities": 0}
    for (state_slug, city_slug), paths in sorted(groups.items()):
        key = f"{state_slug}/{city_slug}"
        if cp is not None and cp.is_done(key):
            continue
        counts = _process_city(state_slug, city_slug, sorted(paths), engine)
        total["inserted"] += counts["inserted"]
        total["updated"] += counts["updated"]
        total["cities"] += 1
        if cp is not None:
            cp.mark_done(key, inserted=counts["inserted"], updated=counts["updated"])
    log.info("martindale.load_done", **total)
    print(
        f"Martindale load complete: {total['cities']} cities "
        f"({total['inserted']} inserted, {total['updated']} updated)."
    )


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
        firm_urls = {(r.get("additional_data") or {}).get("firm_profile_url") for r in records}
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
        r.setdefault("scraped_at", dt.datetime.now(dt.UTC))
        ad = r.get("additional_data") or {}
        fpu = ad.get("firm_profile_url")
        if fpu:
            r["source_url"] = fpu
        r.setdefault("source_url", "")

    # Phase 5: upsert
    engine = create_engine(settings.db_url, connect_args={"timeout": 30})
    with Session(engine) as session:
        counts = upsert_firm_source_records(session, firm_records)
    log.info("martindale.upsert_done", **counts)
    print(
        f"Martindale pilot complete: {len(records)} attorneys -> "
        f"{len(firm_records)} firms ({counts['inserted']} inserted, "
        f"{counts['updated']} updated)."
    )


def run_enrich(
    *,
    only_pending: bool = True,
    limit: int | None = None,
) -> None:
    """Resumable firm-profile enrichment pass.

    Walks Martindale rows with a `firm_profile_url`. For each row
    whose `enrichment_status` is not `'enriched'`, fetches the
    profile and parses the rich fields via `parse_firm_profile_full`.
    Sets `enrichment_status='enriched' | 'failed' | 'no_profile'`,
    so re-running picks up where we left off.
    """
    from sqlalchemy.orm.attributes import flag_modified

    settings = get_settings()
    configure_logging()
    engine = create_engine(settings.db_url, connect_args={"timeout": 30})
    with Session(engine) as session:
        # 1) Mark non-subscriber rows (no firm_profile_url) so future
        # passes skip them deterministically.
        all_martindale = session.scalars(
            select(FirmSourceRecord).where(
                FirmSourceRecord.source == "martindale",
                FirmSourceRecord.enrichment_status.is_(None),
            )
        ).all()
        marked_no_profile = 0
        for r in all_martindale:
            if not (r.additional_data or {}).get("firm_profile_url"):
                r.enrichment_status = "no_profile"
                marked_no_profile += 1
        session.commit()
        log.info(
            "martindale.enrich_marked_no_profile",
            count=marked_no_profile,
        )

        # 2) Targets: rows with firm_profile_url AND not yet enriched.
        q = select(FirmSourceRecord).where(
            FirmSourceRecord.source == "martindale",
        )
        if only_pending:
            q = q.where(
                (FirmSourceRecord.enrichment_status.is_(None))
                | (FirmSourceRecord.enrichment_status == "pending")
                | (FirmSourceRecord.enrichment_status == "failed")
            )
        targets = [
            r for r in session.scalars(q).all() if (r.additional_data or {}).get("firm_profile_url")
        ]
        if limit:
            targets = targets[:limit]
        log.info("martindale.enrich_start", to_enrich=len(targets))

        enriched = failed = 0
        with MartindaleScraper() as scraper:
            for r in targets:
                fpu = r.additional_data["firm_profile_url"]
                try:
                    path = scraper.fetch_one(
                        fpu,
                        method="GET",
                        bucket="firm_profiles",
                        filename=_firm_profile_filename(fpu),
                    )
                    if path is None:
                        raise RuntimeError("fetch_one returned no path")
                    profile = parse_firm_profile_full(_read_gz_bytes(path))
                except Exception as exc:
                    log.error(
                        "martindale.enrich_failed",
                        firm=r.name_raw,
                        error=str(exc)[:300],
                    )
                    r.enrichment_status = "failed"
                    failed += 1
                    session.commit()
                    continue

                _apply_enrichment(r, profile)
                r.enrichment_status = "enriched"
                for col in (
                    "contacts",
                    "offices",
                    "practice_areas_raw",
                    "practice_areas_matched",
                    "practice_areas_unmatched",
                    "additional_data",
                    "firm_descriptions",
                ):
                    flag_modified(r, col)
                session.commit()
                enriched += 1

        log.info("martindale.enrich_done", enriched=enriched, failed=failed)
        print(
            f"Enrichment complete: {enriched} enriched, {failed} failed, "
            f"{marked_no_profile} marked no_profile."
        )


def _firm_profile_filename(url: str) -> str:
    parts = [p for p in url.rstrip("/").split("/") if p]
    for p in parts:
        if "-" in p and any(c.isdigit() for c in p):
            return p
    return "unknown"


def _apply_enrichment(row: FirmSourceRecord, profile: dict) -> None:
    """Splice the parsed profile fields onto an existing row."""
    if profile.get("primary_city"):
        row.primary_city = profile["primary_city"]
    if profile.get("primary_state"):
        row.primary_state = profile["primary_state"]
    if profile.get("primary_postal_code"):
        row.primary_postal_code = profile["primary_postal_code"]
    if profile.get("year_established") is not None:
        row.year_founded = profile["year_established"]
    if profile.get("office_count") is not None:
        row.office_count = profile["office_count"]
    if profile.get("firm_short_description"):
        row.firm_short_description = profile["firm_short_description"]
    if profile.get("firm_descriptions"):
        row.firm_descriptions = profile["firm_descriptions"]
    if profile.get("firm_website_url") and not row.website_raw:
        row.website_raw = profile["firm_website_url"]
    ad = dict(row.additional_data or {})
    if profile.get("firm_website_is_sponsored") is not None:
        ad["firm_website_is_sponsored"] = profile["firm_website_is_sponsored"]
    if profile.get("office_size_label_raw") is not None:
        ad["office_size_label_raw"] = profile["office_size_label_raw"]
    row.additional_data = ad

    # Practice areas — profile is canonical for Martindale.
    if profile.get("practice_areas"):
        row.practice_areas_raw = list(profile["practice_areas"])
        from legal_sourcing.normalize import (
            get_taxonomy,
            normalize_practice_area,
        )

        tax = get_taxonomy()
        matched: set[str] = set()
        unmatched: set[str] = set()
        for s in row.practice_areas_raw:
            slug = tax.match(s)
            if slug:
                matched.add(slug)
            else:
                n = normalize_practice_area(s)
                if n:
                    unmatched.add(n)
        row.practice_areas_matched = sorted(matched)
        row.practice_areas_unmatched = sorted(unmatched)

    # Subscriber flag — reaching this code path means we successfully
    # fetched a profile, so by definition this firm is a subscriber.
    row.is_subscriber = True

    # People-count cross-check: SRP attorney_count undercounts large
    # firms (Starnes SRP=42 vs profile=56). Prefer the larger value.
    if (
        profile.get("people_count") is not None
        and (row.attorney_count or 0) < profile["people_count"]
    ):
        row.attorney_count = profile["people_count"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("pilot", "full", "load", "enrich"),
        help="pilot = fixed-cohort SRP sweep. full = national state->city "
        "sweep (resumable, per-city commit). load = re-parse fetched "
        "pages from disk (no network). enrich = firm-profile enrichment.",
    )
    parser.add_argument(
        "--max-pages-per-city",
        type=int,
        default=3,
        help="(pilot/full) Cap the per-city page walk. 0 = no cap. "
        "Default 3 for pilot; pass 0 for a true full sweep.",
    )
    parser.add_argument(
        "--states",
        type=str,
        default="all",
        help="(full only) Comma-separated state slugs (e.g. "
        "'arizona,new-york') or 'all' for the whole US. Default 'all'.",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="(load only) data/raw/martindale/{date} partition to parse. "
        "Defaults to the most recent.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="(full only) Ignore the checkpoint and re-process every city.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="(enrich only) Cap the number of rows enriched in this run.",
    )
    args = parser.parse_args()
    if args.mode == "pilot":
        cap = args.max_pages_per_city if args.max_pages_per_city > 0 else None
        run_pilot(max_pages_per_city=cap)
    elif args.mode == "full":
        cap = args.max_pages_per_city if args.max_pages_per_city > 0 else None
        run_full(
            states=parse_states_arg(args.states),
            max_pages_per_city=cap,
            resume=not args.no_resume,
        )
    elif args.mode == "load":
        run_load(date_str=args.date)
    elif args.mode == "enrich":
        run_enrich(limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
