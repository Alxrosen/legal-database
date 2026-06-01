# DEVELOPER.md — orientation for engineers and database users

If you're picking up this codebase or querying the SQLite that comes
out of it, start here. Conventions, schema, and how to actually run
the thing. Read `AGENTS.md` too if you're driving an AI agent against
this repo.

## What this project does

Scrapes U.S. law-firm directories (AZ State Bar, Martindale-Hubbell,
FindLaw — Justia and others are planned), normalizes the data, and
resolves the same firm across sources into a canonical record. The
output is a SQLite database (Postgres-portable) optimized for
deal-sourcing research, not for republication.

Arizona is the pilot region. The architecture is source-agnostic —
adding a new source is one parser file, one scraper file, and one
pipeline file.

---

## Quickstart

### Setup

```powershell
# Windows / PowerShell. Installs uv, creates .venv, syncs deps,
# copies .env.example -> .env.
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

On non-Windows: install [uv](https://docs.astral.sh/uv/), then:

```bash
uv sync --extra dev
cp .env.example .env
# Edit .env — at minimum, set AZBAR_API_PASSWORD if you want to scrape AZ Bar.
```

### First-time DB

```bash
uv run alembic upgrade head            # applies all migrations
uv run python scripts/seed_practice_areas.py
```

### Run a pilot

```bash
# AZ Bar — JSON API. ~600 attorneys in ~1 minute.
uv run python -m legal_sourcing.pipelines.scrape_az_bar pilot \
    --page 1 --page-size 200 --num-pages 3

# Martindale — HTML. 5 cities x 10 pages + firm enrichment ~7 minutes.
uv run python -m legal_sourcing.pipelines.scrape_martindale pilot \
    --max-pages-per-city 10
uv run python -m legal_sourcing.pipelines.scrape_martindale enrich

# FindLaw — HTML, Cloudflare-fronted. ~45 (practice_area, city) combos.
uv run python -m legal_sourcing.pipelines.scrape_findlaw pilot \
    --max-pages-per-combo 5

# Entity resolution (no rescrape needed) — populate MatchReviewQueue
uv run python -m legal_sourcing.resolution.run resolve

# Apply decisions — build canonical Firm + FirmSourceRecordLink rows
uv run python -m legal_sourcing.resolution.apply

# Inspect
uv run python scripts/show_all_sources.py
uv run python scripts/show_match_queue.py
```

### Running full sweeps

The polite-rate scrapers take 30 min to multiple hours per source.
Run them sequentially (NOT in parallel — they'd contend on the
shared SQLite at upsert time). Recommended chain:

```bash
uv run python -m legal_sourcing.pipelines.scrape_az_bar full
uv run python -m legal_sourcing.pipelines.scrape_martindale pilot --max-pages-per-city 0
uv run python -m legal_sourcing.pipelines.scrape_martindale enrich
uv run python -m legal_sourcing.pipelines.scrape_findlaw pilot --max-pages-per-combo 20
uv run python -m legal_sourcing.resolution.run resolve
uv run python -m legal_sourcing.resolution.apply
```

### Tests + lint

```bash
make test        # uv run pytest
make lint        # ruff check
make format      # ruff format + lint --fix
make check       # CI-style: check + format-check, no mutations
```

---

## Architecture: six-layer flow

Each layer is strictly separated from the next; any layer can be
re-run without re-running the earlier ones.

```
1. SCRAPE      fetch raw HTML/JSON, gzip to data/raw/{source}/{date}/
2. PARSE       read raw files from disk, emit structured dicts
3. SOURCE      one FirmSourceRecord per (source, firm-as-seen)
4. RESOLVE     block + score + queue/auto-merge match decisions
5. CANONICAL   Firm + FirmSourceRecordLink (deferred post-M6 sign-off)
6. ENRICH      gap-fill from firm websites, geocoding, etc.
```

Project-wide principles:

- **Raw is sacred.** Scraped bytes are gzipped and never overwritten.
  Each date partition is its own immutable slot.
- **Parsers are pure.** No DB writes, no network, no clock. Given a
  payload and source URL, output is deterministic.
- **Normalize for comparison, not storage.** Every `*_raw` field has
  a `*_normalized` companion. Both stay.
- **Decisions are data.** Resolution components and threshold
  snapshots persist in `match_review_queue`, so threshold tuning
  doesn't trigger re-scoring or re-scraping.

---

## Schema overview

Full table-by-table reasoning is in [`docs/schema.md`](docs/schema.md).
Three groups of tables matter:

### Source-layer tables (one source's view)

| Table | Purpose |
|---|---|
| `firm_source_records` | One row per `(source, firm-as-the-source-sees-it)`. Holds raw + normalized fields, contacts/offices as JSON lists, practice areas, enrichment status. |
| (no parallel person-source table) | Per-attorney data lives in `firm_source_records.contacts` JSON; person tables are populated only when resolution promotes a representative attorney to canonical. |

Key columns on `firm_source_records`:

```
id, source, source_firm_id, source_url, scraped_at, raw_payload_path

# Identity / contact (raw + normalized)
name_raw, name_normalized
website_raw, website_normalized      (bare-domain canonicalization)
phone_raw, phone_normalized          (E.164)
year_founded, attorney_count, source_last_updated_at

# Denormalized for queryability (indexed)
primary_city, primary_state, primary_postal_code

# Subscriber-only enrichment (Martindale firm profile pass)
office_count, is_subscriber, firm_short_description,
firm_descriptions (JSON: [{heading, text}]),
enrichment_status (pending|enriched|no_profile|failed)

# Multi-valued
contacts (JSON list — per-attorney from this source)
offices  (JSON list — per-office from this source)
practice_areas_raw / _matched / _unmatched
additional_data (JSON — source-specific extras)

# Deactivation markers (Retired / Inactive / Deceased)
deactivation_status
```

### Canonical-layer tables (cross-source, populated post-M6 sign-off)

| Table | Purpose |
|---|---|
| `firms` | The canonical firm. `field_provenance` JSON tracks which `firm_source_record` supplied each field. |
| `firm_source_record_links` | M-to-M between `firms` and `firm_source_records`. Carries `link_method` (`auto` / `manual`) and a back-pointer to the resolution decision. |
| `offices` | Canonical physical locations per firm. |
| `persons` + `firm_persons` | Canonical attorneys + time-bounded firm affiliations. `FirmPerson.title_rank` drives the primary-contact selection. |

### Resolution + taxonomy

| Table | Purpose |
|---|---|
| `match_review_queue` | Every scored pair with its components, threshold snapshot, status (`pending` / `auto_approved` / `approved` / `rejected`). |
| `practice_areas` | Canonical taxonomy seeded from `data/reference/practice_areas.yaml`. |
| `practice_area_aliases` | Strings that map to canonical areas (matched by exact equality on the normalized form). |
| `firm_practice_areas` | M-to-M with per-source provenance. |
| `unmatched_practice_areas` | Aggregated unmatched normalized strings with counts; periodic review feeds new aliases. |

---

## Where data lives

```
data/raw/{source}/{YYYY-MM-DD}/[bucket/]<file>.<ext>.gz     # gitignored
data/raw/{source}/{YYYY-MM-DD}/[bucket/]<file>.json         # sidecar
data/reference/practice_areas.yaml                           # committed
data/reference/findlaw_practice_areas.csv                    # committed
data/legal_sourcing.sqlite                                   # gitignored
```

Each scraped payload has a sidecar JSON with URL, status, headers,
SHA-256, and fetch timestamp. The raw file is gzipped HTML or JSON
depending on the source.

`tests/fixtures/<source>/recon/` carries the committed sample data
used by parser tests — these are real responses captured during the
reconnaissance passes and are part of the regression backstop.

---

## Common queries

Connect to the SQLite directly (any tool — `sqlite3` CLI, DB Browser,
DBeaver, etc.):

```bash
sqlite3 data/legal_sourcing.sqlite
```

### How many firms per source?

```sql
SELECT source, COUNT(*) FROM firm_source_records GROUP BY source;
```

### Firms in a city

```sql
SELECT source, name_raw, website_raw, phone_normalized
FROM firm_source_records
WHERE primary_city = 'Phoenix' AND primary_state = 'AZ'
ORDER BY name_raw;
```

### Multi-attorney firms

```sql
SELECT source, name_raw, attorney_count, year_founded
FROM firm_source_records
WHERE attorney_count >= 5
ORDER BY attorney_count DESC;
```

### Cross-source name overlap (pre-resolution)

```sql
SELECT name_normalized, GROUP_CONCAT(DISTINCT source)
FROM firm_source_records
WHERE name_normalized IS NOT NULL
GROUP BY name_normalized
HAVING COUNT(DISTINCT source) >= 2;
```

### Firms tagged with a priority practice area

`practice_areas_matched` is a JSON array of canonical slugs:

```sql
SELECT name_raw, source, practice_areas_matched
FROM firm_source_records
WHERE practice_areas_matched LIKE '%personal-injury%'
   OR practice_areas_matched LIKE '%medical-malpractice%';
```

### Resolution decisions

```sql
-- Auto-approved matches at the current thresholds
SELECT m.score_total, a.source, a.name_raw, b.source, b.name_raw
FROM match_review_queue m
JOIN firm_source_records a ON a.id = m.source_record_a_id
JOIN firm_source_records b ON b.id = m.source_record_b_id
WHERE m.status = 'auto_approved'
ORDER BY m.score_total DESC;

-- Mid-confidence pairs for human review
SELECT m.score_total, m.score_components, a.name_raw, b.name_raw
FROM match_review_queue m
JOIN firm_source_records a ON a.id = m.source_record_a_id
JOIN firm_source_records b ON b.id = m.source_record_b_id
WHERE m.status = 'pending'
ORDER BY m.score_total DESC LIMIT 50;
```

### Practice-area review queue

```sql
SELECT normalized_value, count, last_seen_at
FROM unmatched_practice_areas
ORDER BY count DESC LIMIT 50;
```

---

## Adding a new source

The pattern is the same for every source:

1. **Recon first.** Document the source in
   `docs/data_sources/<source>.md` — endpoint URLs, required headers,
   ToS / robots posture, pagination shape, card shape. Run a
   `scripts/recon_<source>.py` that fetches a small slice and saves
   fixtures. **Do not write the parser before recon completes.**
2. **Scraper.** Subclass `BaseScraper` in
   `src/legal_sourcing/scrapers/<source>.py`. Override
   `SOURCE_NAME`, `BASE_URL`, `RATE_LIMIT_RPS`, `WORKERS`,
   `BURST_CAPACITY`, `INITIAL_RATE_LIMIT_RPS`, `RATE_RAMP_SECONDS`,
   `_default_headers()`. If the site has bot detection that
   returns 2xx with a challenge body, override
   `_check_for_block_response()` to raise.
3. **Parser.** Subclass `BaseParser` in
   `src/legal_sourcing/parsers/<source>.py`. Implement
   `parse_bytes(payload, *, source_url) -> list[dict]`. Output dicts
   should match `FirmSourceRecord` columns (raw fields only — the
   pipeline normalizes).
4. **Pipeline.** `src/legal_sourcing/pipelines/scrape_<source>.py`.
   Orchestrates scrape -> parse -> normalize -> aggregate -> upsert.
   Re-use `normalize_record`, `aggregate_by_firm`, and
   `upsert_firm_source_records` from `scrape_az_bar.py` — they're
   source-agnostic.
5. **Tests.** Parser tests against committed fixtures; scraper
   tests against `respx`-mocked HTTP.
6. **Run a pilot.** Show the resulting `FirmSourceRecord` rows
   before scaling up.

---

## Configuration

Everything reads from `.env` via `pydantic-settings`. See
`.env.example` for the full list. Important knobs:

```
DB_PATH=./data/legal_sourcing.sqlite        # SQLite path
RAW_DATA_DIR=./data/raw                     # where scrapers write
PROCESSED_DATA_DIR=./data/processed

USER_AGENT=legal-sourcing-research/0.1      # global fallback
RATE_LIMIT_RPS=8.0                          # global fallback
MAX_WORKERS=6                               # global fallback
REQUEST_TIMEOUT_SECONDS=30
LOG_LEVEL=INFO

# Source-specific secrets
AZBAR_API_PASSWORD=                         # see docs/data_sources/az_bar_reference.md
```

Per-source RPS / workers OVERRIDE the global defaults via class
attributes on the scraper. Globals are fallbacks only.

---

## Documentation conventions

If you change anything, also update:

- **`docs/schema.md`** — model changes.
- **`docs/assumptions.md`** — append a dated entry for any design
  decision that might need revisiting. Format: date / assumption /
  why / trigger to revisit / enforced where.
- **`docs/data_sources/<source>.md`** — selector or endpoint
  changes. Mark confirmed claims with `CONFIRMED YYYY-MM-DD`.

The data-source docs in particular drift quickly — every source's
selectors will go stale eventually. The `CONFIRMED <date>`
annotations are how we know what's still trustworthy.

---

## What's in `scripts/`

Throwaway-or-occasional CLI tools, separate from the production
pipelines:

| Script | Purpose |
|---|---|
| `setup.ps1` | One-time dev-env install (Windows). |
| `seed_practice_areas.py` | YAML -> DB upsert for the canonical taxonomy. |
| `recon_azbar.py` / `recon_martindale.py` / `recon_findlaw.py` | Per-source reconnaissance dumpers. |
| `recon_martindale_firm_profiles.py` | Sample firm-profile pages for selector validation. |
| `reparse_martindale_recon.py` | Re-run extractors against saved gz pages (no re-fetch). |
| `renormalize_addresses.py` | Re-run `normalize_record` on every existing row. |
| `backfill_primary_address.py` | Derive `primary_city` / `primary_state` / `primary_postal_code` from the offices JSON. |
| `show_*.py` | Read-only inspectors for each source / the match queue. |
| `inspect_*.py` | Recon fixture inspectors. |

---

## What's NOT here (future work)

- Canonical `Firm` + `FirmSourceRecordLink` row creation (deferred
  post-M6 threshold sign-off).
- Practice-area review CLI (spec'd in `docs/assumptions.md`).
- Justia / Avvo sources.
- Full-directory sweeps — pilot-sized slices only so far.
- Geocoding, year-founded enrichment beyond Martindale subscribers.
- Postgres migration (SQLite is the pilot; schema is portable).

---

## Where to read next

- **Architecture rationale and rules:** `AGENTS.md` (one level up
  from the layer-by-layer schema in this file)
- **Schema details:** `docs/schema.md`
- **Design decisions log:** `docs/assumptions.md`
- **Per-source operational notes:** `docs/data_sources/*.md`
