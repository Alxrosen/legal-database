"""Resolve each firm website's redirect target -> ``WebsiteEnrichment.redirect_domain``.

For every ``WebsiteEnrichment`` row we record the bare domain the site actually
resolves to after following HTTP redirects. That value tells a true name-variant /
rebrand / acquisition apart from a mis-attributed website:

* ``shermanhoward.com`` 301-redirects to ``taftlaw.com`` (Sherman & Howard was
  acquired by Taft) -> the records are the SAME firm. ``redirect_domain`` captures
  that the domain now lives at ``taftlaw.com``.
* ``zellaw.com`` resolves to itself (Zelms Erlich Lenkov) -> an unrelated firm
  (Maxwell & Morgan) whose record carries ``zellaw.com`` is a mis-attribution,
  not a merge.

The resolution layer uses ``redirect_domain`` for the website must-link /
mis-attribution guard (see docs/canonizer_handoff.md).

``redirect_domain`` semantics:

* ``NULL``                       -- not resolved yet
* ``redirect_domain == website`` -- resolves to itself (no cross-domain redirect)
* ``redirect_domain != website`` -- redirects elsewhere (rebrand / alias / parked
  / mis-attributed); inspect which firm owns the target domain

Two sources, cheapest first:

1. **derive** (default, NO network): ``redirect_domain = normalize_url(resolved_url)``.
   ``resolved_url`` is the final URL the website-enrichment crawler already
   followed redirects to (it fetches with ``follow_redirects=True``), so for every
   already-crawled domain the answer is in the DB for free.
2. **fetch** (network): for rows with no ``resolved_url`` (never crawled, or
   unreachable at crawl time), actively issue an HTTP request that follows
   redirects and record where it lands.

Idempotent + resumable: only fills rows where ``redirect_domain IS NULL`` unless
``--refresh``. Updates are written in batches with the standard WAL lock-retry.
This pass writes ONLY the ``redirect_domain`` column -- it never touches the
crawler's ``resolved_url`` / ``fetched_at`` / ``http_status``.

CLI::

    uv run python -m legal_sourcing.pipelines.resolve_redirects derive
    uv run python -m legal_sourcing.pipelines.resolve_redirects derive --refresh
    uv run python -m legal_sourcing.pipelines.resolve_redirects derive --dry-run
    uv run python -m legal_sourcing.pipelines.resolve_redirects fetch --limit 500 --workers 8
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from legal_sourcing.config import get_settings
from legal_sourcing.db import make_engine
from legal_sourcing.models import WebsiteEnrichment
from legal_sourcing.normalize.url import normalize_url
from legal_sourcing.utils.logging import configure_logging, get_logger

log = get_logger(__name__)

#: Identify ourselves politely when actively resolving redirects.
_USER_AGENT = "legal-sourcing-redirect-resolver/1.0 (+https://bowstreetllc.com)"

#: Write updates to the shared DB in batches of this size (WAL-friendly).
_FLUSH_EVERY = 1000


def resolved_domain_from_url(resolved_url: str | None) -> str | None:
    """Bare domain of an already-resolved URL, or None. The redirect target is
    just the registered-domain form of the crawler's ``resolved_url``."""
    return normalize_url(resolved_url)


def resolve_one(website: str, client: httpx.Client) -> str | None:
    """Actively resolve ``website`` over HTTP (following redirects) and return the
    bare domain it lands on, or None if unreachable.

    Tries HTTPS then HTTP, HEAD then GET. The final URL after redirects is
    ``response.url`` regardless of the status code, so a 404/405 at the
    destination still yields the landing domain.
    """
    for scheme in ("https", "http"):
        url = f"{scheme}://{website}"
        resp: httpx.Response | None = None
        try:
            resp = client.head(url)
        except httpx.HTTPError:
            try:
                resp = client.get(url)
            except httpx.HTTPError:
                continue
        final = normalize_url(str(resp.url))
        if final:
            return final
    return None


def _flush(engine: Engine, updates: list[dict[str, object]]) -> int:
    """Bulk-update ``redirect_domain`` by primary key, retrying the WAL writer
    lock with exponential backoff (mirrors pipelines/enrich_websites._flush)."""
    if not updates:
        return 0
    for attempt in range(6):
        try:
            with Session(engine) as s:
                s.execute(update(WebsiteEnrichment), updates)
                s.commit()
            return len(updates)
        except OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt == 5:
                raise
            log.warning("redirects.flush_retry", attempt=attempt + 1, pending=len(updates))
            time.sleep(0.5 * 2**attempt)
    return 0


def derive(
    *, refresh: bool = False, limit: int | None = None, dry_run: bool = False
) -> dict[str, int]:
    """Populate ``redirect_domain`` from each row's existing ``resolved_url`` (no
    network). The cheap, primary path -- every already-crawled domain is covered."""
    configure_logging()
    engine = make_engine()
    counts: Counter[str] = Counter(
        {"scanned": 0, "updated": 0, "redirect": 0, "same_site": 0, "unchanged": 0, "unresolved": 0}
    )
    pending: list[dict[str, object]] = []

    with Session(engine) as s:
        stmt = select(
            WebsiteEnrichment.id,
            WebsiteEnrichment.website,
            WebsiteEnrichment.resolved_url,
            WebsiteEnrichment.redirect_domain,
        )
        if not refresh:
            stmt = stmt.where(WebsiteEnrichment.redirect_domain.is_(None))
        if limit:
            stmt = stmt.limit(limit)

        for row_id, website, resolved_url, current in s.execute(
            stmt.execution_options(yield_per=5000)
        ):
            counts["scanned"] += 1
            target = resolved_domain_from_url(resolved_url)
            if target is None:
                counts["unresolved"] += 1
                continue
            if target == current:
                counts["unchanged"] += 1
                continue
            pending.append({"id": row_id, "redirect_domain": target})
            counts["redirect" if target != website else "same_site"] += 1
            if len(pending) >= _FLUSH_EVERY and not dry_run:
                counts["updated"] += _flush(engine, pending)
                pending = []

    if pending and not dry_run:
        counts["updated"] += _flush(engine, pending)
    log.info("redirects.derive_done", dry_run=dry_run, **counts)
    return dict(counts)


def fetch(
    *, refresh: bool = False, limit: int | None = None, workers: int = 8, dry_run: bool = False
) -> dict[str, int]:
    """Actively resolve rows that ``derive`` could not (no ``resolved_url``) by
    issuing real HTTP requests that follow redirects."""
    configure_logging()
    settings = get_settings()
    engine = make_engine()
    counts: Counter[str] = Counter(
        {"scanned": 0, "updated": 0, "redirect": 0, "same_site": 0, "unchanged": 0, "unresolved": 0}
    )

    with Session(engine) as s:
        stmt = select(WebsiteEnrichment.id, WebsiteEnrichment.website).where(
            WebsiteEnrichment.resolved_url.is_(None)
        )
        if not refresh:
            stmt = stmt.where(WebsiteEnrichment.redirect_domain.is_(None))
        if limit:
            stmt = stmt.limit(limit)
        targets = [(rid, web) for rid, web in s.execute(stmt).all() if web]

    log.info("redirects.fetch_start", to_resolve=len(targets), workers=workers, dry_run=dry_run)
    pending: list[dict[str, object]] = []
    timeout = httpx.Timeout(settings.request_timeout_seconds, connect=10.0)
    with (
        httpx.Client(
            headers={"User-Agent": _USER_AGENT}, timeout=timeout, follow_redirects=True
        ) as client,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        futs = {pool.submit(resolve_one, web, client): (rid, web) for rid, web in targets}
        for fut in as_completed(futs):
            rid, web = futs[fut]
            counts["scanned"] += 1
            try:
                target = fut.result()
            except Exception as exc:  # network / parse errors: log and skip the domain
                log.warning("redirects.fetch_error", website=web, error=str(exc))
                target = None
            if target is None:
                counts["unresolved"] += 1
                continue
            pending.append({"id": rid, "redirect_domain": target})
            counts["redirect" if target != web else "same_site"] += 1
            if len(pending) >= _FLUSH_EVERY and not dry_run:
                counts["updated"] += _flush(engine, pending)
                pending = []

    if pending and not dry_run:
        counts["updated"] += _flush(engine, pending)
    log.info("redirects.fetch_done", dry_run=dry_run, **counts)
    return dict(counts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=("derive", "fetch"))
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="Re-resolve rows that already have a redirect_domain (e.g. after the "
        "crawler updated resolved_url).",
    )
    ap.add_argument("--limit", type=int, default=None, help="Cap the number of rows processed.")
    ap.add_argument("--workers", type=int, default=8, help="(fetch) concurrent HTTP workers.")
    ap.add_argument("--dry-run", action="store_true", help="Resolve but do not write to the DB.")
    args = ap.parse_args()

    if args.mode == "derive":
        counts = derive(refresh=args.refresh, limit=args.limit, dry_run=args.dry_run)
    else:
        counts = fetch(
            refresh=args.refresh, limit=args.limit, workers=args.workers, dry_run=args.dry_run
        )

    suffix = " (dry-run)" if args.dry_run else ""
    print(
        f"resolve_redirects {args.mode} complete{suffix}:\n"
        f"  scanned        : {counts.get('scanned', 0):,}\n"
        f"  updated        : {counts.get('updated', 0):,}\n"
        f"    -> redirects : {counts.get('redirect', 0):,}\n"
        f"    -> same site : {counts.get('same_site', 0):,}\n"
        f"  unchanged      : {counts.get('unchanged', 0):,}\n"
        f"  unresolved     : {counts.get('unresolved', 0):,}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
