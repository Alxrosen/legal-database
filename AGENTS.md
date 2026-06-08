# AGENTS.md — orientation for AI agents

You are working in **legal-deal-sourcing**, a Python pipeline that
scrapes U.S. law-firm directories, normalizes the data, and resolves
firms across sources. Read this once before doing anything; it
captures the project's rules of the road and several non-obvious
gotchas surfaced over prior milestones.

## Project mission, one paragraph

Build a database of U.S. law firms by scraping public directories
(AZ State Bar, Martindale-Hubbell, FindLaw, Justia, + a generic
state-bar scraper), enriching from firms' own websites, normalizing
the data, and resolving the same firm across sources into a canonical
record. The DB is for internal deal-sourcing research — **not** for
republication. Arizona was the pilot region (scraping has gone
national); the architecture is source-agnostic and the data model is
designed so adding a new source is a localized change.

## Working style the user expects

These come from the original project handoff and are reinforced
across the assumption log:

- **Stop at milestone boundaries.** Don't chain milestones. After
  each one, summarize what was built, show key files, explain
  decisions, and wait for sign-off.
- **Ask before guessing.** When something is underspecified — a
  library choice, a schema field, a threshold — surface it as a
  question. Don't silently pick.
- **Show, don't tell.** When checking in, show actual file contents
  and command output. Summaries are not enough.
- **Small commits.** Each logical sub-step gets its own commit with
  a descriptive message. Avoid mega-commits.
- **Flag risks loudly.** If a source's ToS looks dicey, a library is
  unmaintained, or a design choice will be painful to reverse, say so
  explicitly.
- **Diagnostic discipline.** Don't conclude a hypothesis (e.g.
  "the API key rotated") without explicit evidence. See the AZ Bar
  Userid lesson in `docs/assumptions.md`.

## Architecture in six layers

Strictly separated so any layer can be re-run without touching the
others:

```
1. SCRAPE       fetch raw HTML/JSON, gzip to data/raw/{source}/{date}/
                  -> no parsing, no DB writes
2. PARSE        read raw files from disk -> structured dicts
                  -> pure functions; no network, no clock, no DB
3. SOURCE       one FirmSourceRecord per (source, firm-as-source-sees-it)
                  -> raw + normalized side-by-side
4. RESOLVE      block candidate pairs, score on multiple signals,
                  auto-merge above threshold, queue mid-confidence
5. CANONICAL    Firm + FirmSourceRecordLink (deferred post-M6)
                  -> per-field provenance in field_provenance JSON
6. ENRICH       fill gaps from firm websites, geocoding, etc.
```

Principles enforced everywhere:
- Scraping and parsing are strictly separated. Re-parsing must
  never require re-scraping.
- Raw bytes are stored compressed and **never overwritten** (each
  date partition is its own slot).
- All resolution decisions and score components are persisted, so
  re-running with new thresholds doesn't require re-scoring or
  re-scraping.
- Normalization is for COMPARISON, not storage. Always keep
  `*_raw` and `*_normalized` columns side by side.

## Repo layout

```
src/legal_sourcing/
├── config.py                   pydantic-settings; .env-driven
├── scrapers/                   one module per source + base.py
├── parsers/                    one module per source + base.py
├── normalize/                  text, name (+looks_like_firm), phone,
│                                address, url, practice_areas, title_rank
├── models/                     SQLAlchemy 2.0 (incl. website_enrichment)
├── pipelines/                  CLI orchestrators (scrape_*, enrich_websites)
├── resolution/                 blocking, scoring, run, apply
├── enrichment/                 website_extract.py (firm-site cascade)
├── state_bars.py               per-state StateBarConfig registry
└── utils/                      logging, helpers
# scrapers/ + parsers/ each: one module per source + base.py, plus the
# generic state_bar.py and (scrapers/) website.py.

migrations/                     Alembic
data/
├── raw/                        gzipped raw payloads (gitignored)
├── processed/                  (gitignored)
└── reference/                  small lookups (committed)
docs/
├── schema.md                   table-by-table reasoning
├── assumptions.md              decision log — APPEND, don't rewrite
├── data_sources/               one file per source w/ CONFIRMED annotations
│   ├── az_bar_reference.md   martindale.md   findlaw_reference.md
│   ├── justia.md   avvo.md (blocked)
│   ├── state_bars.md         (§13 = per-state triage)
│   └── firm_websites.md      (§12–13 = website-enrichment rules + pilots)
scripts/                        one-off CLI tools (recon_*, show_*, etc.)
tests/                          pytest; fixtures committed
```

## How things are run

```bash
# Setup (one-time)
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1

# Scrape a source (each writes raw to data/raw/{source}/{date}/
# and FirmSourceRecord rows to the SQLite DB)
uv run python -m legal_sourcing.pipelines.scrape_az_bar pilot \
    --page 1 --page-size 200 --num-pages 3
uv run python -m legal_sourcing.pipelines.scrape_martindale pilot \
    --max-pages-per-city 10
uv run python -m legal_sourcing.pipelines.scrape_martindale enrich
uv run python -m legal_sourcing.pipelines.scrape_findlaw pilot \
    --max-pages-per-combo 5

# NATIONAL full sweep (state->city discovery, per-city commit,
# resumable via a checkpoint file under data/processed/). Re-running
# the SAME command resumes; --no-resume re-processes everything.
# --states defaults to "all" (50 + DC); pass a comma list to scope it.
uv run python -m legal_sourcing.pipelines.scrape_martindale full \
    --states all --max-pages-per-city 0 --rps 0.75   # 0=no page cap; --rps tunes rate
uv run python -m legal_sourcing.pipelines.scrape_martindale enrich  # after full
uv run python -m legal_sourcing.pipelines.scrape_findlaw full \
    --states all --max-pages-per-combo 0
uv run python -m legal_sourcing.pipelines.scrape_justia full --states all

# State bars (generic, config-driven; WY is the wired reference state):
uv run python -m legal_sourcing.pipelines.scrape_state_bar pilot --state wy
uv run python -m legal_sourcing.pipelines.scrape_state_bar full  --state wy

# Firm-website content enrichment (keyed by website; pilot/run/load):
uv run python -m legal_sourcing.pipelines.enrich_websites pilot --websites a.com,b.com
uv run python -m legal_sourcing.pipelines.enrich_websites run    # full ~27k-site crawl
uv run python -m legal_sourcing.pipelines.enrich_websites load   # re-extract from disk

# Recover a crashed/interrupted full run from disk (NO network):
uv run python -m legal_sourcing.pipelines.scrape_martindale load [--date YYYY-MM-DD]
uv run python -m legal_sourcing.pipelines.scrape_findlaw  load [--date YYYY-MM-DD]
uv run python -m legal_sourcing.pipelines.scrape_justia   load [--date YYYY-MM-DD]

# Derive primary_city/state/postal from offices JSON (run before resolution):
uv run python -m scripts.backfill_primary_address

# Entity resolution — populate MatchReviewQueue
uv run python -m legal_sourcing.resolution.run resolve \
    --auto-merge 85 --review 60 --store-min 40

# Apply resolution decisions — build canonical Firm + Link rows
uv run python -m legal_sourcing.resolution.apply

# Migrations
uv run alembic revision --autogenerate -m "..."
uv run alembic upgrade head

# Tests
make test       # or: uv run pytest
make check      # ruff lint + format check
```

## Non-obvious gotchas you will run into

Read these before debugging anything that looks weird:

### selectolax — depth vs union vs ordering

- `Node.iter()` returns DIRECT CHILDREN ONLY, not descendants. For a
  recursive walk write your own DFS (see
  `parsers/martindale._walk_dfs`).
- Union CSS selectors like `tree.css("h2, div.x")` **do not return
  results in document order** — they group by selector arm. If you
  need document order across element types, use the DFS walker.
- selectolax does NOT support `:contains()` — use a Python-side
  filter instead.

### SQLAlchemy JSON columns

In-place mutations of `Mapped[list]` / `Mapped[dict]` JSON columns
are **not** auto-detected as dirty. Assignment alone often isn't
enough either when the new value points to the same objects. Use
`sqlalchemy.orm.attributes.flag_modified(row, "column_name")` to
force the UPDATE. Pattern used in
`scripts/renormalize_addresses.py` and the Martindale enrichment.

### Address normalization

`usaddress` is a statistical tagger. Feeding it just `"Dothan AL"`
(no street, no comma) mis-tags as `street="al dothan"`. We
preserve `city_raw` / `state_raw` from the source directly into
`offices[].normalized` and only run usaddress on the street
portion. See `pipelines.scrape_az_bar.normalize_record`.

The street tag order in usaddress output is non-deterministic if you
iterate `_STREET_TAGS` as a set. Use the explicit
`_STREET_TAG_ORDER` tuple.

City names from AZ Bar come UPPERCASED ("PHOENIX"). We title-case
them in `normalize_record` so they match Martindale's "Phoenix".

### Source-specific quirks

- **AZ Bar API** requires BOTH `Password: <uuid>` AND `Userid:
  publictools` headers. Missing either returns 401 with an empty
  body. Do NOT conclude "Password rotated" without verifying the
  current value works in DevTools first.
- **Martindale** caps page enumeration at **167 pages** regardless
  of declared total. Phoenix declares 17,522 but `data-max=167`.
  Mega-cities need filtered queries to access more than ~5K
  attorneys via this route.
- **Martindale state pages list the whole METRO, not just in-state
  cities.** `/by-location/district-of-columbia-lawyers/` returns
  ~180 city slugs including MD/VA suburbs (`aberdeen-proving-ground`,
  etc.). Harmless — those cities also appear under their own states
  and the upsert is idempotent on `(name, street)` — but it means a
  national `full` run re-fetches some cities under multiple states.
  Wasteful, not wrong.
- **`scrape_martindale full` does NOT fetch firm profiles.** It does
  the city sweep + card-level upsert only. Firm-profile enrichment
  (website fallback, descriptions, year founded, headcount) is the
  separate `enrich` mode — run it AFTER `full`. Folding profiles into
  `full` would add one fetch per unique firm and balloon a national
  run by hundreds of thousands of requests.
- **Martindale "Office Size"** is firm headcount, NOT number of
  offices. Parser nulls `office_count` when "Office Size" is
  within ~25% of `people_count` (almost always — they're the same
  number).
- **Martindale's documented selectors are drift-prone.** The
  reference doc said `a.profile-website-body[href]` for the
  website button; reality is `a.webstats-website-click[href]`.
  Parser tries both in order. Always confirm selectors against
  saved HTML during recon.
- **FindLaw** sits behind Cloudflare. Use a browser-shaped UA (NO
  "bot" / "research" / contact string in the UA). 0.33 RPS,
  burst=1, single-threaded. Treat any 403/429/503 OR 200 with
  "cf-mitigated"/"Just a moment" body as fatal — do NOT retry. See
  `scrapers.findlaw.FindLawCloudflareChallenge`.
- **FindLaw cards are firm-level** (no individual attorneys per
  card). Same firm appears under multiple practice-area URLs —
  dedup by `(name_normalized, primary_street_normalized)`.
- **Practice-area slugs differ across sources.** Martindale's
  `practice_areas_raw` items are free-text ("Civil Litigation");
  FindLaw's are URL slugs ("motor-vehicle-accidents-plaintiff");
  AZ Bar nests them. The `normalize/practice_areas.py` matcher
  expects free-text and uses exact match on the normalized form
  (`law/attorney/lawyer/legal/...` stripped, singularized).
  Unmatched values flow to `unmatched_practice_areas` for review.

### Pagination

- **Martindale**: use `input.goToPage[data-max]` for the deterministic
  last page; fall back to max numbered `data-page` on the
  pagination `<a>`s; use `a.arrow[rel="next"]` as per-iteration
  defensive stop. See `parsers/martindale.extract_page_meta`.
- **AZ Bar list endpoint**: `Result.Results = []` is the clean
  end-of-data signal (declared `TotalCount` is global).
- **FindLaw**: `nav[aria-label="Pagination"]` + numbered `<li>`s
  for last_page + `a.fl-pagination-button[rel="next"]` for next.
  Page size is 40 (not 20 as the doc speculated).

### Python 3.14 — urlparse is strict

`urllib.parse.urlparse` raises `ValueError("Invalid IPv6 URL")` on
malformed URLs (stray `[`, broken IPv6 literals) on Python 3.14,
where older Pythons were lenient. Scraped URLs are full of junk, so
**never call `urlparse` on scraped data without guarding it**. Use
`legal_sourcing.normalize.url.safe_urlparse` (returns `None` instead
of raising). `normalize_url` already wraps it.

This bug killed a full AZ Bar overnight sweep: it fetched all 35,864
detail pages, then crashed in the normalize phase on one attorney's
malformed `FirmURL`, losing the whole run's DB write. Hence the
`load` mode below.

### `load` mode — recover a fetch without re-scraping

All three sources now have `load`. `scrape_az_bar load` re-parses
`data/raw/az_bar/{date}/detail/*.json.gz`; `scrape_martindale load`
and `scrape_findlaw load` re-parse the `city/...` page tree, grouped
back into per-city units (the same durable grain the live `full` run
commits). All three run parse -> normalize -> aggregate -> upsert with
NO network. Use them to recover from a late crash.

**`full` mode is now incremental + resumable, which is the primary
crash-safety mechanism** (load is the fallback). `full` commits one
city at a time inside its own Session and records each completed city
in a checkpoint file under `data/processed/{source}_full_progress.json`.
A crash, Ctrl-C, or reboot loses at most one city; re-running the same
`full` command skips everything already in the checkpoint. This is why
a days-long national run survives interruption without re-fetching.

Lesson for unattended runs: don't chain sources with `set -e` (one
crash aborts everything downstream). Run each source as an
independent, individually-logged step that continues past failures.
The two sources write to the SAME `{source}_full_progress.json`-style
files but DIFFERENT names, so Martindale and FindLaw `full` can run
concurrently; do NOT run two `full` passes for the SAME source at once
(they'd race the one checkpoint file).

### Canonical apply step

`resolution/apply.py` is the only writer of the `firms` and
`firm_source_record_links` tables. It clears + rebuilds both on
every run. **Do not write to these tables from anywhere else** —
the source of truth is `MatchReviewQueue` + per-source
`FirmSourceRecord`.

The CURRENT `apply.py` is a placeholder: simple per-field source
precedence (`DEFAULT_SOURCE_PRIORITY`) + a DISTINCT-attorney count
(`_aggregate_attorney_count`: email→name→id dedup; NOT the old MAX).
**This is being replaced** by the iterative truth-discovery fusion
(docs/assumptions.md 2026-06-03): per-(source+enrichment-level)
reliability, recency-weighted, union-first multi-valued fields,
argmax-confidence for scalars, website + `website_enrichment` as a
high-trust source. That fusion is the next major build.

### Rate limiting

- `RateLimiter` is a token bucket with optional ramp (`initial_rps`,
  `ramp_seconds`). Sleeps are OUTSIDE the lock — concurrent
  acquirers can wait in parallel.
- Each scraper overrides `RATE_LIMIT_RPS`, `WORKERS`,
  `BURST_CAPACITY`, `INITIAL_RATE_LIMIT_RPS`, `RATE_RAMP_SECONDS`.
- Defaults are POLITE. Don't yank them aggressive without user
  sign-off. **Martindale takes a per-run `--rps` override** (default
  0.5; tuned to 0.75 — it 429s at ≥~1.0). Tune up, back off on the
  first sustained 429.

### robots.txt

Default `ROBOTS_POLICY="warn"` — robots is still fetched, disallows
log a warning but the fetch proceeds. Per-source override to
`"block"` if you want hard enforcement.

## When you change anything, also update

- **`docs/assumptions.md`** — every constraint that might need
  revisiting later. Format: date / assumption / why / trigger to
  revisit / enforced where. Append, don't rewrite.
- **`docs/schema.md`** — any model change.
- **`docs/data_sources/<source>.md`** — selector or endpoint changes
  for that source. Mark verified claims with `CONFIRMED YYYY-MM-DD`.
- The relevant migration if you touched a SQLAlchemy model.

## What's done so far (state 2026-06-04)

- **4 directory sources** with national `full` + `load` + resumable per-unit
  checkpoints (`geo.US_STATE_SLUGS`, `pipelines/_checkpoint.py`): AZ Bar,
  Justia, FindLaw (complete) + **Martindale** (in progress — see run state).
- **Data-quality guards** (parser/normalize level; applied via `load` re-parse,
  no rescrape): no directory records its OWN domain (`strip_self_domain`);
  aggregator/social domains dropped (`AGGREGATOR_DOMAINS` + `is_aggregator_domain`);
  email-as-website rejected in `normalize_url`; Martindale office state
  backfilled from the swept URL (`STATE_SLUG_TO_ABBR` + `_backfill_office_state`).
- **State bars** (NEW): ONE **generic config-driven** scraper, not 50 bespoke
  ones — `state_bars.py` (`StateBarConfig` registry), `scrapers/state_bar.py`,
  `parsers/state_bar.py` (small per-state extractors sharing one spine),
  `pipelines/scrape_state_bar.py` (`pilot`/`full`/`load`, A–Z last-name sweep,
  per-term checkpoint; reuses the AZ Bar normalize/aggregate/upsert). **Wyoming**
  wired + pilot-validated as the reference state. Recon tooling:
  `scripts/recon_state_bars.py` (+ `_forms.py --platforms`, `recon_imis.py`).
  Triage of all 50+DC in `docs/data_sources/state_bars.md` §13: public bar data
  is mostly thin (name+city only), gated (members-only / iMIS), or JS-rendered —
  only a minority expose firm/address/phone publicly. **State-bar full scraping
  is PAUSED** (low ROI); wire the few "worth it" states on demand.
- **Website (firm-site) content enrichment** (NEW, BUILT): extracts the
  EBITDA-proxy signals from firms' own sites.
  `enrichment/website_extract.py` = a pure, platform-agnostic extraction
  CASCADE (legal-relevance gate, headcount cascade stated→profile-links→
  heading-roles→solo→unknown, offices/years/phones/signals, platform hint,
  `discover_internal_pages`). `models/website_enrichment.py` + migration
  `852fd3019b0b` = a table **keyed by normalized website** (crawl each unique
  site once; re-runs = set-difference on `enriched_at`). `scrapers/website.py`
  + `pipelines/enrich_websites.py` = producer/worker pipeline (`pilot`/`run`/
  `load`; one bulk-upsert committer; raw-to-disk so `load` re-extracts with no
  re-fetch). ~27,252 distinct sites in scope; pilot-validated against known
  firms (firm_websites.md §13/§13.5). **Full `run` is ON HOLD**; the LLM
  `description_generated` layer is deferred (needs `ANTHROPIC_API_KEY`, unset).
- **Canonical resolution design = ITERATIVE TRUTH DISCOVERY** (supersedes the
  old MAX / fixed-precedence note). Per research (assumptions.md 2026-06-03):
  jointly estimate per-(firm,field) truth + per-**source** reliability, where
  source = origin + enrichment level (website / martindale_enriched /
  martindale_card / findlaw / state bars / justia); recency-weighted; union-first
  for multi-valued fields; NOT fetch-order-deterministic (intentional).
  `_aggregate_attorney_count` already counts DISTINCT attorneys (email→name→id
  dedup; `looks_like_firm` is now shared in `normalize/name.py`). **The
  truth-discovery fusion in `apply.py` is NOT built yet** — `apply.py` is still
  the simple per-field precedence version. This is the next major task.
- **Concurrency hardening**: DB in **WAL** + `connect_args timeout=30` +
  `upsert_firm_source_records` retries on `database is locked` (rollback +
  re-apply) — concurrent scrapers coexist.
- **Martindale `--rps` knob**: `scrape_martindale full --rps N` overrides the
  sustained rate per run (default 0.5). Tuned 2026-06-04 — 0.75 clean, 1.25 drew
  sustained 429s → settled at **0.75 RPS**.
- **254+ passing tests**; `make check` clean for everything we touched.

### DB / scrape run state (2026-06-04)

- **AZ Bar**: complete — 28,171 firms. **Justia**: complete — 42,301.
  **FindLaw**: complete — ~7,570.
- **Martindale**: IN PROGRESS, running detached at **0.75 RPS**, logging to
  `data/logs/martindale_full_2026-06-04.log`, checkpoint at **~2,577 cities**
  (`data/processed/martindale_full_progress.json`). Resume with the same
  `full --states all --max-pages-per-city 0 --rps 0.75`.
- **website_enrichment**: table exists with a handful of pilot rows; the full
  ~27k-site `run` has NOT been launched.
- `primary_city/state/postal_code` are still **0** in the DB — run
  `python -m scripts.backfill_primary_address` before resolution (unlocks
  name_state blocking + geo scoring).
- `firms` / `match_review_queue` are EMPTY (no canonical build yet).

### Next-run / pre-resolution playbook

1. Let **Martindale `full`** finish (running at 0.75; watchdog supervises;
   re-run the same command to resume).
2. **`scrape_martindale load`** then **`scrape_martindale enrich`** — load
   BEFORE enrich (load re-parses city pages and would clobber enrich's
   websites; enrich adds website/phone/descriptions that `full` lacks).
3. **`python -m scripts.backfill_primary_address`** — derive `primary_*`.
4. Run / refresh **website enrichment** (`enrich_websites run`) so the website
   source is populated before canonical fusion.
5. Decide government/court-entity handling (assumptions 2026-06-02; e.g. `.gov`
   AG offices form false clusters — the website extractor flags `.gov/.edu`).
6. **Build the truth-discovery fusion in `apply.py`** (assumptions 2026-06-03),
   then **`resolution.run resolve`** → **`resolution.apply`**.

## Parallel Claude sessions (multi-agent hygiene)

Multiple Claude sessions work this repo at once: **Mastermind** (coordinator /
integration), **Websites** (site enrichment → `source="website"`), **Canonizer**
(resolution), **Cleanser** (project auditor — read-only), **Monitor** (Martindale
scrape watcher — read-only, a Mastermind fork), **Enricher** (Martindale
firm-profile `enrich` — recovers nameless-row names), and **Fixer** (office-address
parser fix + `backfill_primary_address`). Each runs in a **dedicated `git
worktree` on its own branch** (`Mastermind` / `Websites` / `Canonizer` /
`Cleanser` / `Monitor` / `Enricher` / `Fixer`) — a shared working tree collides
(lost edits, staging races, duplicate Alembic heads). They share **one live WAL
database**: each
worktree's `.env` points `DB_PATH` (+ `RAW_DATA_DIR`/`PROCESSED_DATA_DIR`) at the
**absolute** path of the main checkout's DB (the relative default would make each
worktree silently open its own empty DB).

Rules of engagement:
- **Commit only specific files** (`git add <files>`, never `-A`/`.`) and stay in
  your file lane; a parallel session may leave **uncommitted WIP in files you
  didn't write — do NOT commit or revert it**.
- **All DB access via `legal_sourcing.db.make_engine()`** (busy_timeout=30s + WAL).
- **Disjoint writes** (read anything; write only your lane):
  Martindale/Enricher/Fixer/Websites all write `firm_source_records` but **disjoint by
  `source` and/or column-group** — Martindale (the live scrape) + Enricher (firm-profile
  enrich: names/contacts/descriptions/year/offices) + Fixer (`offices[].normalized`
  re-derive + `primary_city/state/postal_code`/`office_count`) on `source="martindale"`
  rows; Websites on `source="website"` rows. Canonizer→`firms`/`firm_source_record_links`/
  `match_review_queue`. **Cleanser + Monitor→READ-ONLY** (findings/log-watch only — never
  write data tables).
- **Migrations are Mastermind-only**, run only when scrapes are quiesced (DDL against
  a live writer is the one genuinely unsafe operation; a plain add-column is fine).
- **Coordinate via `COORDINATION.md`** on `main` (pull to read; edit your own section
  + push to post). Full rationale: `docs/assumptions.md` "2026-06-04 — Multi-agent
  shared database".

## What's intentionally NOT done

- **Canonical truth-discovery fusion** — the `apply.py` rewrite (design in
  assumptions.md 2026-06-03) is the next major task; current `apply.py` is the
  simple-precedence placeholder.
- **Website-enrichment full run** (built, ON HOLD) + the **LLM description
  layer** (needs `ANTHROPIC_API_KEY`).
- **Most state bars** — harness + WY exist; most jurisdictions are low-ROI
  (thin / gated / JS per state_bars.md §13); wire worthwhile ones on demand.
- **Avvo**: hard-blocked by Cloudflare (needs TLS-impersonation/headless;
  deferred). Practice-area review CLI (spec'd, not built). Geocoding. Postgres
  migration. Interactive human-review CLI for the `pending` match-queue band.

## Running full sweeps in background

National `full` runs are multi-day at polite rates. Launch each **detached via
WMI** so it survives the session / compaction —
`Invoke-CimMethod Win32_Process Create -Arguments @{CommandLine="cmd /c
<venv-python> -m legal_sourcing.pipelines.scrape_<src> full --states all ...
>> data\logs\<src>_full_<date>.log 2>&1"; CurrentDirectory=<proj>}`. Supervise
with a persistent **Monitor** tailing the log, matching REAL signatures —
`HTTP/1\.1 [45][0-9][0-9]|429 Too Many|403 Forbidden|cloudflare|scrape.http_retryable|Traceback|database is locked`
— NOT bare `403`/`429` (those false-match page counts / totals). Resumability
(checkpoints) is the real safety net: re-run the same `full` command to resume.

Concurrency: per-source rows are disjoint, the DB is **WAL**, and the upsert
retries on lock — so concurrent `full` runs coexist (Martindale + website
enrichment have run together fine). Tune throughput with Martindale's `--rps`
and back off on the first sustained 429 (its ceiling is ~0.75; >1.0 throttles).

Order: Martindale `load` must run BEFORE `martindale enrich` (enrich adds
websites a later `load` would clobber); `enrich` only touches existing rows.

## Test discipline

Every parser and normalizer has tests against committed fixtures
under `tests/fixtures/<source>/recon/`. New parsers should follow
that pattern. Don't delete fixtures — they're the regression
backstop.
