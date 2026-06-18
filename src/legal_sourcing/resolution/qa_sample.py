"""Surveyor's QA-loop engine: sample canonical firms, re-verify against the live
web, and capture the bad cases as committed regression fixtures.

The loop is agentic (Surveyor fans out sub-agents on ``/goal``); this module is
the *deterministic* engine they drive. It never decides "this value is wrong on
its own" for the has-website case -- re-running the extractor over the same pages
reproduces the same (possibly wrong) value, so a fresh-vs-baseline diff only
catches *drift*. Catching extractor BUGS (e.g. attorney_count 9 vs the true 3)
needs judgment, which is the sub-agent's job: this module hands each sub-agent an
evidence packet (stored baseline + a fresh re-extraction + the page's visible
text + deterministic suspicion flags + the staged raw HTML) and the sub-agent
reads the page to decide ground truth, then calls ``capture``.

Reuse, never reinvent:
* ``resolution.sample_eval._random_firm_websites`` -- random identity-website draw
* ``pipelines.enrich_websites.crawl_firm`` -- the PRODUCTION fetch + extract (so a
  "fresh" row is byte-for-byte what production would store), plus ``_bucket`` /
  ``_read_gz`` to recover the just-fetched raw HTML for staging
* ``enrichment.website_extract.extract_site`` / ``_visible_text`` -- the real
  extractor + its notion of a page's visible text (so the test and the agent
  judge against exactly what the extractor sees)
* ``resolution.identity.is_identity_website`` + ``normalize.firm_name`` -- the
  identity / low-quality-name gates

Subcommands::

    qa_sample sample --random 8            # has-website: fetch+re-extract+diff
    qa_sample sample --no-website 8        # website-less: heuristic candidates for search
    qa_sample capture --run <ts> --domain gagemathers.com \
        --field attorney_count --expected 3 --note "team page lists 3 named attorneys"
    qa_sample apply-rescrape [--dry-run]   # enriched_at = NULL for queued domains

Always run as::

    ~/.local/bin/uv run --directory <surveyor-worktree> python -m \
        legal_sourcing.resolution.qa_sample sample --random 8
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from legal_sourcing.config import get_settings
from legal_sourcing.db import make_engine
from legal_sourcing.enrichment.website_extract import _tree, _visible_text
from legal_sourcing.normalize.firm_name import firm_name_core, is_low_quality_firm_name
from legal_sourcing.pipelines.enrich_websites import _bucket, _read_gz, crawl_firm
from legal_sourcing.resolution.identity import is_identity_website
from legal_sourcing.resolution.sample_eval import _random_firm_websites
from legal_sourcing.scrapers.website import FirmWebsiteScraper

# ---------------------------------------------------------------------------
# Paths (anchored to the worktree root so artifacts land correctly regardless of
# CWD; qa_sample.py is at <root>/src/legal_sourcing/resolution/qa_sample.py).

_ROOT = Path(__file__).resolve().parents[3]
QA_DIR = _ROOT / "data" / "qa"
GOLDEN_CSV = _ROOT / "data" / "eval" / "extraction_golden.csv"
FIXTURES_DIR = _ROOT / "tests" / "fixtures" / "qa_cases"
TO_REVIEW = QA_DIR / "to_review.jsonl"
RESCRAPE_QUEUE = QA_DIR / "rescrape_queue.jsonl"

GOLDEN_HEADER = ["case_id", "website", "field", "expected_value", "now_year", "note"]

# Roles promoted into a committed fixture (extract_site keys off these names).
FIXTURE_ROLES = ("home", "attorneys", "team", "about", "offices", "locations")
# Per-role visible-text cap in findings packets (enough for an attorney roster
# without dumping a 250KB page into a judge's context).
EXCERPT_CHARS = 2500


def read_fixture_pages(case_dir: Path) -> list[tuple[str, bytes]]:
    """Load a committed fixture's pages as (role, html-bytes) for ``extract_site``.

    Fixtures are stored gzipped (``<role>.html.gz`` — the production raw-cache
    form, ~5-10x smaller in git); plain ``<role>.html`` is accepted as a fallback
    for any legacy fixture. Single loader shared by both regression tests so the
    storage format lives in exactly one place.
    """
    pages: list[tuple[str, bytes]] = []
    for role in FIXTURE_ROLES:
        gz = case_dir / f"{role}.html.gz"
        raw = case_dir / f"{role}.html"
        if gz.exists():
            pages.append((role, gzip.decompress(gz.read_bytes())))
        elif raw.exists():
            pages.append((role, raw.read_bytes()))
    return pages


# ---------------------------------------------------------------------------
# Golden-field registry: single source of truth shared with
# tests/test_extraction_golden.py. field -> (accessor, parse_expected, equal).


def _as_int(v: Any) -> int | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    return None if s in ("", "none", "null") else int(s)


def _as_bool(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes", "y")


def _as_set(v: Any) -> set[str]:
    items = v if isinstance(v, (set, list, tuple)) else re.split(r"[|;]", str(v))
    return {s.strip().lower() for s in items if str(s).strip()}


GOLDEN_FIELDS: dict[str, tuple[Any, Any, Any]] = {
    "attorney_count": (lambda s: s.attorney_count, _as_int, lambda a, b: a == b),
    "year_founded": (lambda s: s.year_founded, _as_int, lambda a, b: a == b),
    "years_in_operation": (lambda s: s.years_in_operation, _as_int, lambda a, b: a == b),
    "office_count": (lambda s: s.office_count, _as_int, lambda a, b: a == b),
    "primary_city": (
        lambda s: s.primary_city,
        lambda v: (str(v).strip() or None) if v else None,
        lambda a, b: (a or "").strip().lower() == (b or "").strip().lower(),
    ),
    "primary_state": (
        lambda s: s.primary_state,
        lambda v: (str(v).strip() or None) if v else None,
        lambda a, b: (a or "").strip().upper() == (b or "").strip().upper(),
    ),
    "is_law_related": (lambda s: s.is_law_related, _as_bool, lambda a, b: bool(a) == bool(b)),
    "url_verification_status": (
        lambda s: s.url_verification_status,
        lambda v: str(v).strip(),
        lambda a, b: (a or "") == (b or ""),
    ),
    "name_normalized": (
        lambda s: s.name_normalized,
        lambda v: str(v).strip(),
        lambda a, b: (a or "") == (b or ""),
    ),
    "practice_areas": (
        lambda s: {x.lower() for x in (s.practice_areas or [])},
        _as_set,
        lambda a, b: set(a) == set(b),
    ),
}


# ---------------------------------------------------------------------------
# Small IO helpers


def _now() -> datetime:
    return datetime.now(UTC)


def _ts() -> str:
    return _now().strftime("%Y%m%d-%H%M%S")


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(_ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def _jsonish(v: Any) -> Any:
    """website_enrichment / firms JSON columns come back as str on raw SQL."""
    if isinstance(v, str) and v[:1] in ("[", "{"):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


# ---------------------------------------------------------------------------
# DB reads (raw SQL: the live `firms` table carries columns not on the ORM model)


def _baseline(session: Session, website: str) -> dict[str, Any] | None:
    row = (
        session.execute(text("SELECT * FROM website_enrichment WHERE website = :w"), {"w": website})
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _firms_context(session: Session, website: str) -> dict[str, Any] | None:
    rows = (
        session.execute(
            text(
                "SELECT id, name, attorney_count, year_founded, practice_areas, city, state "
                "FROM firms WHERE website_normalized = :w"
            ),
            {"w": website},
        )
        .mappings()
        .all()
    )
    if not rows:
        return None
    # A domain should map to one canonical firm; if more, surface the largest by id
    # set and note the fan-out (a possible under-merge worth a glance).
    primary = dict(rows[0])
    for k in ("practice_areas", "city", "state"):
        primary[k] = _jsonish(primary.get(k))
    primary["firm_row_count"] = len(rows)
    return primary


# ---------------------------------------------------------------------------
# Staging: recover the raw HTML crawl_firm just wrote, for fixtures + excerpt


def _stage_pages(
    website: str, run_dir: Path
) -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
    """Recover the raw HTML crawl_firm just wrote -> stage as plain .html (for
    fixtures) and return per-role visible text (the distilled signal a judge
    reads — esp. the attorneys/team roster, which a home-biased blob truncates)
    plus per-role RAW byte sizes (so the sanity-monitor can watch HTML size)."""
    base = get_settings().raw_data_dir / "firm_websites"
    bucket = _bucket(website)
    gzs = sorted(base.glob(f"*/{bucket}/*.html.gz"), key=lambda p: p.stat().st_mtime)
    if not gzs:
        return {}, {}, {}
    # Latest gz per role wins (mtime-sorted; later overwrites earlier).
    by_role: dict[str, Path] = {}
    for gz in gzs:
        stem = gz.name[: -len(".html.gz")]
        role = "home" if stem == "home" else re.sub(r"\d+$", "", stem)
        by_role[role] = gz
    out_dir = run_dir / "pages" / bucket
    out_dir.mkdir(parents=True, exist_ok=True)
    staged: dict[str, str] = {}
    page_text: dict[str, str] = {}
    page_bytes: dict[str, int] = {}
    for role, gz in by_role.items():
        html = _read_gz(gz)
        fp = out_dir / f"{role}.html"
        fp.write_bytes(html)
        staged[role] = _rel(fp)
        page_text[role] = _visible_text(_tree(html))[:EXCERPT_CHARS]
        page_bytes[role] = len(html)
    return staged, page_text, page_bytes


# ---------------------------------------------------------------------------
# Comparison + suspicion flags

# (label, fresh_row_key, baseline_col) over the columns both rows share.
_DRIFT_FIELDS = [
    ("attorney_count", "attorney_count_min", "attorney_count_min"),
    ("office_count", "office_count", "office_count"),
    ("years_in_operation", "years_in_operation_min", "years_in_operation_min"),
    ("primary_city", "primary_city", "primary_city"),
    ("primary_state", "primary_state", "primary_state"),
    ("is_law_related", "is_law_related", "is_law_related"),
    ("url_verification_status", "url_verification_status", "url_verification_status"),
]


def _drift(fresh: dict[str, Any], baseline: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not baseline:
        return []
    out = []
    for label, fk, bk in _DRIFT_FIELDS:
        fv, bv = fresh.get(fk), baseline.get(bk)
        if fv != bv:
            out.append({"field": label, "baseline": bv, "fresh": fv})
    fa = sorted({str(x).lower() for x in (fresh.get("practice_areas") or [])})
    ba = sorted({str(x).lower() for x in (_jsonish(baseline.get("practice_areas")) or [])})
    if fa != ba:
        out.append({"field": "practice_areas", "baseline": ba, "fresh": fa})
    return out


def _suspicion(
    website: str,
    fresh: dict[str, Any],
    firms_ctx: dict[str, Any] | None,
    baseline: dict[str, Any] | None,
    now_year: int,
) -> list[str]:
    flags: list[str] = []
    status = fresh.get("url_verification_status")
    if status in ("legal_but_mismatched", "not_a_law_firm"):
        flags.append(f"url_{status}")
    # A normalized "domain" with userinfo/whitespace or no dot is itself a bug —
    # e.g. "reid@reidnathan.com" (an email leaked into website_normalized).
    if "@" in website or any(c.isspace() for c in website) or "." not in website:
        flags.append("malformed_domain")
    if baseline is None:
        flags.append("no_enrichment_baseline")
    if not is_identity_website(website) or website.endswith(".org"):
        flags.append("aggregator_or_org_domain")
    ac = fresh.get("attorney_count_min")
    oc = fresh.get("office_count")
    if ac is not None and ac >= 20 and (oc or 0) <= 1:
        flags.append("high_count_single_office")
    if ac is not None and fresh.get("attorney_count_confidence") in ("low", "none"):
        flags.append("weak_count_confidence")
    yf = (firms_ctx or {}).get("year_founded")
    if yf is not None and (yf < 1850 or yf > now_year):
        flags.append("implausible_year_founded")
    rd = fresh.get("redirect_domain")
    if rd and rd != website:
        flags.append("redirect_domain_differs")
    nm = (firms_ctx or {}).get("name")
    if nm and is_low_quality_firm_name(nm, host=website):
        flags.append("low_quality_firm_name")
    # NB: a domain mapping to >1 firm row is an under-MERGE signal — Canonizer's
    # lane, NOT a Surveyor finding (handoff SCOPE). firm_row_count stays in the
    # firms context for awareness, but we deliberately do not flag on it.
    return flags


# ---------------------------------------------------------------------------
# sample


def _inspect_has_website(
    scraper: FirmWebsiteScraper,
    session: Session,
    website: str,
    run_dir: Path,
    now_year: int,
) -> dict[str, Any]:
    fresh = crawl_firm(scraper, website)
    baseline = _baseline(session, website)
    firms_ctx = _firms_context(session, website)
    status = fresh.get("url_verification_status")
    packet: dict[str, Any] = {
        "case_id": _bucket(website),
        "website": website,
        "status": "unreachable" if status == "unreachable" else "fetched",
        "http_status": fresh.get("http_status"),
        "now_year": now_year,
        "baseline_enriched_at": str(baseline.get("enriched_at")) if baseline else None,
        "firms": firms_ctx,
        "baseline": {
            k: _jsonish(baseline.get(k)) if baseline else None
            for k in (
                "attorney_count_min",
                "office_count",
                "years_in_operation_min",
                "primary_city",
                "primary_state",
                "is_law_related",
                "url_verification_status",
                "practice_areas",
                "redirect_domain",
            )
        },
        "fresh": {
            "attorney_count_min": fresh.get("attorney_count_min"),
            "attorney_count_method": fresh.get("attorney_count_method"),
            "attorney_count_confidence": fresh.get("attorney_count_confidence"),
            "office_count": fresh.get("office_count"),
            "years_in_operation_min": fresh.get("years_in_operation_min"),
            "primary_city": fresh.get("primary_city"),
            "primary_state": fresh.get("primary_state"),
            "is_law_related": fresh.get("is_law_related"),
            "url_verification_status": status,
            "practice_areas": fresh.get("practice_areas"),
        },
    }
    if packet["status"] == "unreachable":
        packet["staged_pages"] = {}
        packet["page_text"] = {}
        packet["page_bytes"] = {}
        packet["drift"] = []
        packet["suspicion"] = ["unreachable_live"]
        return packet
    staged, page_text, page_bytes = _stage_pages(website, run_dir)
    packet["staged_pages"] = staged
    packet["page_text"] = page_text
    packet["page_bytes"] = page_bytes
    packet["total_html_bytes"] = sum(page_bytes.values())
    packet["drift"] = _drift(fresh, baseline)
    packet["suspicion"] = _suspicion(website, fresh, firms_ctx, baseline, now_year)
    return packet


def _targeted_candidates(session: Session, n: int) -> list[str]:
    """N identity-website firms whose STORED signals already look suspect — a
    higher-yield draw than random once the common bugs are mapped. Cohorts:
    mis-attributed/non-firm verification, cross-domain redirect, high count in a
    single office, .org website, malformed (userinfo) domain, implausible founding
    year. Excludes the known-deferred needs_render (thin/JS) class. The packet's
    own suspicion flags re-derive the reason, so we just print the cohort mix."""
    rows = session.execute(
        text(
            """
            SELECT DISTINCT we.website,
              CASE
                WHEN we.url_verification_status IN ('legal_but_mismatched','not_a_law_firm') THEN 'url_mismatch'
                WHEN we.redirect_domain IS NOT NULL AND we.redirect_domain != we.website THEN 'redirect'
                WHEN we.attorney_count_min >= 30 AND COALESCE(we.office_count,0) <= 1 THEN 'high_count_single_office'
                WHEN we.website LIKE '%.org' THEN 'org_domain'
                WHEN f.website_normalized LIKE '%@%' THEN 'malformed_domain'
                ELSE 'bad_year'
              END AS reason
            FROM website_enrichment we
            JOIN firms f ON f.website_normalized = we.website
            WHERE we.url_verification_status IN ('legal_but_mismatched','not_a_law_firm')
               OR (we.redirect_domain IS NOT NULL AND we.redirect_domain != we.website)
               OR (we.attorney_count_min >= 30 AND COALESCE(we.office_count,0) <= 1)
               OR we.website LIKE '%.org'
               OR f.website_normalized LIKE '%@%'
               OR (f.year_founded IS NOT NULL AND (f.year_founded < 1850 OR f.year_founded > :yr))
            ORDER BY random()
            LIMIT :n
            """
        ),
        {"n": n, "yr": _now().year},
    ).all()
    from collections import Counter

    mix = Counter(r[1] for r in rows)
    print(f"  targeted cohort mix: {dict(mix)}")
    return [r[0] for r in rows]


def _heuristic_candidates(name: str) -> list[str]:
    core = firm_name_core(name)
    if not core:
        return []
    joined = "".join(core)[:40]
    cands = [f"{joined}.com", f"{joined}law.com", f"{joined}lawfirm.com"]
    # de-dup, keep order
    seen, out = set(), []
    for c in cands:
        if c not in seen and len(joined) >= 3:
            seen.add(c)
            out.append(c)
    return out


def _inspect_no_website(session: Session, firm: dict[str, Any]) -> dict[str, Any]:
    name = firm.get("name") or ""
    return {
        "firm_id": firm.get("id"),
        "name": name,
        "city": _jsonish(firm.get("city")),
        "state": _jsonish(firm.get("state")),
        "low_quality_name": is_low_quality_firm_name(name),
        "heuristic_candidates": _heuristic_candidates(name),
        "note": "Sub-agent: WebSearch by name+location; accept a domain ONLY on a "
        "strong, location-consistent identity match; else leave empty. Never guess.",
    }


def cmd_sample(args: argparse.Namespace) -> int:
    run_ts = _ts()
    run_dir = QA_DIR / run_ts
    run_dir.mkdir(parents=True, exist_ok=True)
    now_year = _now().year
    engine = make_engine()

    findings: dict[str, Any] = {
        "run": run_ts,
        "now_year": now_year,
        "mode": "no-website" if args.no_website else "has-website",
    }

    with Session(engine) as session:
        if args.no_website:
            rows = (
                session.execute(
                    text(
                        "SELECT id, name, city, state FROM firms "
                        "WHERE (website_normalized IS NULL OR website_normalized = '') "
                        "ORDER BY random() LIMIT :n"
                    ),
                    {"n": args.no_website},
                )
                .mappings()
                .all()
            )
            findings["firms"] = [_inspect_no_website(session, dict(r)) for r in rows]
            print(f"=== QA no-website sample ({len(findings['firms'])}) — run {run_ts} ===")
            for f in findings["firms"]:
                print(
                    f"  firm {f['firm_id']:>7} {f['name'][:42]:42s} "
                    f"cand={f['heuristic_candidates']} lowq={f['low_quality_name']}"
                )
        else:
            if args.domains:
                websites = [d.strip().lower() for d in args.domains.split(",") if d.strip()]
                src = "explicit"
            elif args.targeted:
                websites = _targeted_candidates(session, args.targeted)
                src = "targeted-suspicious"
            else:
                websites = _random_firm_websites(session, args.random, min_records=args.min_records)
                src = "random"
            print(f"=== QA has-website sample ({len(websites)}, {src}) — run {run_ts} ===")
            packets = []
            with FirmWebsiteScraper() as scraper:
                for w in websites:
                    p = _inspect_has_website(scraper, session, w, run_dir, now_year)
                    packets.append(p)
                    drift = f" drift={len(p['drift'])}" if p["drift"] else ""
                    susp = f" !{','.join(p['suspicion'])}" if p["suspicion"] else ""
                    print(
                        f"  {w[:36]:36s} {p['status']:11s} "
                        f"atty={p['fresh']['attorney_count_min']!s:>4} "
                        f"off={p['fresh']['office_count']!s:>3} "
                        f"verif={p['fresh']['url_verification_status']:20s}{drift}{susp}"
                    )
            findings["packets"] = packets

    findings_path = run_dir / "findings.json"
    findings_path.write_text(json.dumps(findings, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {_rel(findings_path)}")
    if not args.no_website:
        n_susp = sum(1 for p in findings.get("packets", []) if p["suspicion"])
        print(
            f"  {len(findings.get('packets', []))} inspected · {n_susp} with suspicion flags · "
            f"staged raw HTML under {_rel(run_dir / 'pages')}/"
        )
    print(
        "  -> Hand packets to sampling sub-agents to JUDGE (read page_text / staged HTML); "
        "confirmed bugs -> `qa_sample capture`."
    )
    return 0


# ---------------------------------------------------------------------------
# capture


def _write_golden_row(
    case_id: str, website: str, field: str, expected: str, now_year: int, note: str
) -> None:
    GOLDEN_CSV.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    if GOLDEN_CSV.exists():
        with GOLDEN_CSV.open(encoding="utf-8", newline="") as f:
            rows = [r for r in csv.DictReader(f)]
    # Replace any existing (case_id, field) row; else append. Keeps the CSV the
    # single golden record per field even if a case is re-captured.
    rows = [r for r in rows if not (r["case_id"] == case_id and r["field"] == field)]
    rows.append(
        {
            "case_id": case_id,
            "website": website,
            "field": field,
            "expected_value": expected,
            "now_year": str(now_year),
            "note": note,
        }
    )
    rows.sort(key=lambda r: (r["case_id"], r["field"]))
    with GOLDEN_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=GOLDEN_HEADER)
        w.writeheader()
        w.writerows(rows)


def cmd_capture(args: argparse.Namespace) -> int:
    if args.field not in GOLDEN_FIELDS:
        print(f"unknown --field {args.field!r}; known: {sorted(GOLDEN_FIELDS)}", file=sys.stderr)
        return 2
    website = args.domain
    case_id = args.case_id or _bucket(website)
    run_dir = QA_DIR / args.run
    src_pages = run_dir / "pages" / _bucket(website)
    if not src_pages.is_dir():
        print(
            f"no staged pages at {_rel(src_pages)} — run `sample` for {website} first "
            f"(staged HTML is required to commit a fixture; we never re-fetch).",
            file=sys.stderr,
        )
        return 2

    # 1. Promote staged raw HTML -> committed fixture, GZIPPED (.html.gz) — the
    #    same on-disk form as the production raw cache (pipelines.enrich_websites),
    #    so the suite reads it exactly like production (run_load / _read_gz) and a
    #    1MB page costs ~100KB in git. read_fixture_pages decompresses for tests.
    dest = FIXTURES_DIR / case_id
    dest.mkdir(parents=True, exist_ok=True)
    promoted = []
    for role in FIXTURE_ROLES:
        sp = src_pages / f"{role}.html"
        if sp.exists():
            (dest / f"{role}.html.gz").write_bytes(gzip.compress(sp.read_bytes()))
            promoted.append(role)
    if "home" not in promoted:
        print(f"refusing to capture {website}: no home page staged", file=sys.stderr)
        return 2

    now_year = args.now_year or _now().year

    # 2. Golden CSV row (the spec handed to Websites; lands RED until fixed).
    _write_golden_row(case_id, website, args.field, str(args.expected), now_year, args.note or "")

    # 3. to-review list (current -> should-be, with evidence) + 4. re-scrape flag.
    current = args.current
    if current is None:
        engine = make_engine()
        with Session(engine) as session:
            bl = _baseline(session, website)
        current = (bl or {}).get(
            {
                "attorney_count": "attorney_count_min",
                "years_in_operation": "years_in_operation_min",
            }.get(args.field, args.field)
        )
    _append_jsonl(
        TO_REVIEW,
        {
            "ts": _now().isoformat(),
            "case_id": case_id,
            "website": website,
            "field": args.field,
            "current_value": current,
            "should_be": args.expected,
            "evidence": args.note or "",
        },
    )
    # de-dup the rescrape queue on domain
    if website not in {r.get("website") for r in _read_jsonl(RESCRAPE_QUEUE)}:
        _append_jsonl(
            RESCRAPE_QUEUE,
            {"ts": _now().isoformat(), "website": website, "reason": f"qa:{args.field}"},
        )

    print(
        f"captured {website} [{args.field}: {current} -> {args.expected}]\n"
        f"  fixture: {_rel(dest)} (roles: {', '.join(promoted)})\n"
        f"  golden : {_rel(GOLDEN_CSV)} (case {case_id})\n"
        f"  queued : to_review + rescrape_queue\n"
        f"  -> commit the fixture + golden row so Websites can make the RED test GREEN."
    )
    return 0


# ---------------------------------------------------------------------------
# apply-rescrape (the ONLY data write Surveyor performs)


def cmd_apply_rescrape(args: argparse.Namespace) -> int:
    domains = sorted({r["website"] for r in _read_jsonl(RESCRAPE_QUEUE) if r.get("website")})
    if not domains:
        print("rescrape queue empty — nothing to do.")
        return 0
    print(f"{len(domains)} domain(s) queued for re-scrape (enriched_at -> NULL):")
    for d in domains:
        print(f"  {d}")
    if args.dry_run:
        print("\n[dry-run] no changes written.")
        return 0
    engine = make_engine()
    with Session(engine) as session:
        res = session.execute(
            text(
                "UPDATE website_enrichment SET enriched_at = NULL WHERE website IN :ds"
            ).bindparams(bindparam("ds", expanding=True)),
            {"ds": domains},
        )
        session.commit()
        print(f"\ncleared enriched_at on {res.rowcount} website_enrichment row(s).")
    return 0


# ---------------------------------------------------------------------------
# CLI


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser(
        "sample", help="draw firms, fetch+re-extract+diff (or list no-website firms)"
    )
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--random", type=int, help="N has-website firms to fetch + verify")
    g.add_argument(
        "--domains",
        help="comma-separated domains to inspect directly (e.g. seed known cases "
        "gagemathers.com,walmart.com)",
    )
    g.add_argument(
        "--targeted",
        type=int,
        help="N firms whose STORED signals look suspect (mis-attribution / redirect / "
        "high-count-single-office / .org / malformed / bad-year) — higher yield than random",
    )
    g.add_argument("--no-website", type=int, help="N website-less firms to surface for search")
    s.add_argument("--min-records", type=int, default=1, help="min source records behind a domain")
    s.set_defaults(func=cmd_sample)

    c = sub.add_parser("capture", help="promote staged HTML -> committed fixture + golden row")
    c.add_argument("--run", required=True, help="sample run timestamp (the data/qa/<ts> dir)")
    c.add_argument("--domain", required=True)
    c.add_argument("--field", required=True, help=f"one of {sorted(GOLDEN_FIELDS)}")
    c.add_argument(
        "--expected", required=True, help="the correct value (ground truth from the page)"
    )
    c.add_argument(
        "--current", default=None, help="the stored (wrong) value; auto-filled if omitted"
    )
    c.add_argument("--note", default="", help="evidence (what the page actually shows)")
    c.add_argument("--case-id", default=None, help="defaults to the domain bucket slug")
    c.add_argument("--now-year", type=int, default=None, help="pin year for stable assertions")
    c.set_defaults(func=cmd_capture)

    r = sub.add_parser("apply-rescrape", help="enriched_at = NULL for queued domains")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_apply_rescrape)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
