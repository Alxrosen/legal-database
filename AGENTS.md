# AGENTS.md — orientation for AI agents

You are working in **legal-deal-sourcing**, a Python pipeline that
scrapes U.S. law-firm directories, normalizes the data, and resolves
firms across sources. Read this once before doing anything; it
captures the project's rules of the road and several non-obvious
gotchas surfaced over prior milestones.

## Project mission, one paragraph

Build a database of U.S. law firms by scraping public directories
(currently AZ State Bar, Martindale-Hubbell, FindLaw), normalizing
the data, and resolving the same firm across sources. The DB is for
internal deal-sourcing research — **not** for republication. Arizona
is the pilot region; the architecture is source-agnostic and the
data model is designed so adding a new source is a localized change.

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
├── normalize/                  text, name, phone, address, url,
│                                practice_areas, title_rank
├── models/                     SQLAlchemy 2.0 (Mapped[] style)
├── pipelines/                  CLI orchestrators (scrape_*.py)
├── resolution/                 blocking, scoring, run.py
├── enrichment/                 (placeholder for canonical-firm gap fill)
└── utils/                      logging, helpers

migrations/                     Alembic
data/
├── raw/                        gzipped raw payloads (gitignored)
├── processed/                  (gitignored)
└── reference/                  small lookups (committed)
docs/
├── schema.md                   table-by-table reasoning
├── assumptions.md              decision log — APPEND, don't rewrite
├── data_sources/               one file per source w/ CONFIRMED annotations
│   ├── az_bar_reference.md
│   ├── martindale.md
│   └── findlaw_reference.md
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
    --states all --max-pages-per-city 0     # 0 = no page cap
uv run python -m legal_sourcing.pipelines.scrape_martindale enrich  # after full
uv run python -m legal_sourcing.pipelines.scrape_findlaw full \
    --states all --max-pages-per-combo 0
uv run python -m legal_sourcing.pipelines.scrape_justia full --states all

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

Source priority for per-field selection is hardcoded in
`DEFAULT_SOURCE_PRIORITY` (Martindale > FindLaw > AZ Bar today).
Override per field via `FIELD_PRECEDENCE_OVERRIDES` if a future
source clearly wins on a specific signal.

`attorney_count` is the MAX across source members (FindLaw cards
are firm-level with 0; Martindale carries firm-wide headcount;
AZ Bar contributes 1 per attorney). This is the best signal short
of a real cross-source attorney unique-ID.

### Rate limiting

- `RateLimiter` is a token bucket with optional ramp (`initial_rps`,
  `ramp_seconds`). Sleeps are OUTSIDE the lock — concurrent
  acquirers can wait in parallel.
- Each scraper overrides `RATE_LIMIT_RPS`, `WORKERS`,
  `BURST_CAPACITY`, `INITIAL_RATE_LIMIT_RPS`, `RATE_RAMP_SECONDS`.
- Defaults are POLITE. Don't yank them aggressive without user
  sign-off.

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

## What's done so far (state 2026-06-03)

- **4 sources** end-to-end with national `full` + `load` + resumable
  per-unit checkpoints (`geo.US_STATE_SLUGS`, `pipelines/_checkpoint.py`):
  AZ Bar, Martindale, FindLaw, **Justia** (new). See run state below.
- **Data-quality guards** (parser/normalize level; apply via a `load`
  re-parse to existing rows — no rescrape):
  - No directory records its OWN domain as a firm website
    (`normalize.url.strip_self_domain`, per parser).
  - Aggregator/social websites dropped (`AGGREGATOR_DOMAINS` +
    `is_aggregator_domain`; applied in `normalize_record`).
  - Email-as-website rejected in `normalize_url` (`@` guard).
  - Martindale office **state backfilled from the swept URL**
    (`STATE_SLUG_TO_ABBR` + `scrape_martindale._backfill_office_state`);
    cards show only the city.
- **Canonical resolution**: `_aggregate_attorney_count` now counts
  DISTINCT attorneys (email -> name -> bar/profile-id dedup; FindLaw
  person-cards counted, firm-cards not) instead of MAX. `justia` added
  to `DEFAULT_SOURCE_PRIORITY` (bottom). Full precedence redesign is
  DECIDED (docs/assumptions.md 2026-06-02: website-TOP tiers, union
  multi-valued, phones union-keep-all, state-bars+justia bottom) but the
  website tier / `name` override / phone-union are NOT yet wired — they
  bundle with the website_enrichment table.
- **DB-lock crash fix**: `upsert_firm_source_records` retries the commit
  on `OperationalError` (3 concurrent scrapers once exceeded the SQLite
  busy-timeout and crashed Martindale).
- **Website enrichment**: DESIGNED + recon-validated (41 sites + team
  pages) — see `docs/data_sources/firm_websites.md` §12 and the
  assumptions entries. NOT built yet (paused right before writing the
  extraction module `enrichment/website_extract.py`). Hybrid:
  heuristic cascade + LLM only for the generated description (needs
  `ANTHROPIC_API_KEY`, not yet added).
- **228 passing tests**; `make check` clean for everything we touched.

### DB / scrape run state (2026-06-03)

- **AZ Bar**: complete — 28,171 firms.
- **Justia**: complete — 42,301 firms (all 51 states).
- **FindLaw**: complete — 7,570 firms / 7,438 distinct (all 51 states;
  finished 2026-06-03). A few DC state-index pages 403'd late (Cloudflare)
  — non-fatal, discovery skipped them.
- **Martindale**: IN PROGRESS / RESUMING. Crashed once mid-California on
  the DB-lock (now fixed); re-launched and resuming from checkpoint
  (~1,954 cities, 82k rows, was only ~5/51 states). Detached via WMI,
  logging to `data/logs/martindale_full_2026-06-02.log`. Re-run the same
  `full` command any time to resume.
- `primary_city/state/postal_code` are still **0** in the DB — they live
  in the `offices` JSON; run `python -m scripts.backfill_primary_address`
  before resolution (unlocks name_state blocking + geo scoring).
- `firms` / `match_review_queue` are EMPTY (no canonical build on the
  current full data yet).

### Next-run / pre-resolution playbook

1. Let **Martindale `full`** finish (resuming; supervise via a watchdog;
   re-run the same command if it stops).
2. **`scrape_<source> load`** per source (no network) to apply the new
   website/state guards to already-scraped rows. Do `martindale load`
   BEFORE `martindale enrich` (load re-parses city pages and would
   clobber enrich's websites otherwise).
3. **`scrape_martindale enrich`** — firm profiles add website/phone/
   descriptions (Martindale `full` has 0 websites).
4. **`python -m scripts.backfill_primary_address`** — derive
   `primary_*` from `offices`.
5. Decide government/court-entity handling (assumptions 2026-06-02).
6. **`resolution.run resolve`** then **`resolution.apply`** (wire the
   precedence redesign first).
7. Build + run **website enrichment** (firm_websites.md §12).

## What's intentionally NOT done

- Practice-area review CLI (spec'd in `docs/assumptions.md`, not
  implemented).
- **Avvo**: hard-blocked by Cloudflare (a full browser header set does
  NOT pass — unlike Justia). Documented in `docs/data_sources/avvo.md`;
  NOT built (needs TLS-impersonation / headless = a "defeat Cloudflare"
  decision, deferred to the user). (Justia IS done.)
- **Website enrichment**: designed + recon-validated, NOT built (next
  task) — `docs/data_sources/firm_websites.md` §12.
- ~49 more state bar associations (planned by Alex; would slot in at the
  bottom of the canonical precedence with AZ Bar).
- Geocoding, year-founded enrichment beyond Martindale subscriber
  pages.
- Postgres migration (SQLite is the pilot DB; schema is
  Postgres-portable).
- Interactive human-review CLI for the `pending` band of the match
  queue (732 pairs sitting there at default thresholds).

## Running full sweeps in background

National `full` runs are MULTI-DAY at polite rates. How we run them
(2026-06-02/03): launch each **detached via WMI** so it survives the
agent session / compaction —
`Invoke-CimMethod Win32_Process Create -Arguments @{CommandLine="cmd /c
<venv-python> -m legal_sourcing.pipelines.scrape_<src> full --states all
... >> data\logs\<src>_full_<date>.log 2>&1"; CurrentDirectory=<proj>}`.
Supervise with a persistent **Monitor** tailing the log for
`full_done|full complete|cloudflare|403|429`. Resumability (checkpoints)
is the real safety net: re-run the same `full` command to resume.

Concurrency: per-source DB rows are disjoint and the upsert now **retries
on `database is locked`**, so concurrent `full` runs are tolerated — but
3 concurrent writers still once exceeded the busy-timeout and crashed
Martindale, so prefer not to pile on a 4th heavy writer; WAL mode is an
un-taken hardening option.

Order: Martindale `load` (re-parse, applies state/website guards) must
run BEFORE `martindale enrich` (enrich adds websites that a later `load`
would clobber); `enrich` only touches rows already in the DB.

## Test discipline

Every parser and normalizer has tests against committed fixtures
under `tests/fixtures/<source>/recon/`. New parsers should follow
that pattern. Don't delete fixtures — they're the regression
backstop.
