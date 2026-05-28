# Schema

This is the data model for the legal-deal-sourcing pilot. SQLite for now;
designed so swapping to Postgres is mechanical (the only Postgres-specific
choice is the JSON column type, which both engines understand transparently
via SQLAlchemy's `JSON`).

## Overall layout

```
                                                  ┌──────────────────┐
              ┌──────────────────────┐            │ practice_areas   │
              │ firm_source_records  │            │ (canonical taxa) │
              │  (one per source-    │            └────────┬─────────┘
              │   firm pair)         │                     │
              └─────────┬────────────┘                     │
                        │                          ┌───────▼──────────┐
                        │ link                     │ practice_area_   │
                        ▼                          │ aliases          │
              ┌──────────────────────┐             └──────────────────┘
              │ firm_source_record_  │
              │ links (M-to-M)       │
              └─────────┬────────────┘
                        │
                        ▼
              ┌──────────────────────┐    ┌──────────┐  ┌──────────┐
              │ firms                │────┤ offices  │  │ persons  │
              │ (canonical)          │    └──────────┘  └────┬─────┘
              │ + field_provenance   │                       │
              └──────────────────────┘                       │
                        │                                    │
                        └───────── firm_persons ─────────────┘
                                       (M-to-M, time-bounded)

       resolution decisions
       ────────────────────
       ┌──────────────────────┐
       │ match_review_queue   │
       │ (every pair scored,  │
       │  status, components) │
       └──────────────────────┘

       periodic-review tables
       ──────────────────────
       ┌──────────────────────┐
       │ unmatched_practice_  │
       │ areas (counted)      │
       └──────────────────────┘
```

## Tables

### `firm_source_records` — preserve each source's view

One row per (source, firm-as-that-source-sees-it). The grain is
firm-level. Contacts (attorneys) and offices (multiple addresses) are
stored as JSON lists on the row so we keep the source's complete view in
one place. The JSON shapes are documented in the model file and are
deliberately stable so splitting them into child tables later is a
mechanical migration.

Key columns:

| column                        | type    | notes                                              |
|-------------------------------|---------|----------------------------------------------------|
| `id`                          | int PK  |                                                    |
| `source`                      | str     | e.g. `"az_bar"`. Indexed.                          |
| `source_firm_id`              | str?    | the source's own ID, when present                  |
| `source_url`                  | str     | URL the row was scraped from                       |
| `scraped_at`                  | dt UTC  | when the underlying scrape ran                     |
| `raw_payload_path`            | str?    | path under `data/raw/` for the gzipped payload     |
| `http_status`                 | int?    |                                                    |
| `name_raw`, `name_normalized` | str     | raw display name + normalized comparison form      |
| `website_raw`, `website_normalized` | str? | bare-domain normalization form for matching      |
| `phone_raw`, `phone_normalized`     | str? | E.164 normalization                              |
| `year_founded`                | int?    | source-reported, when supplied                     |
| `attorney_count`              | int?    | numeric firm-size; descriptor strings stay in `additional_data` |
| `source_last_updated_at`      | dt UTC? | source's own "last updated"; distinct from `scraped_at` |
| `contacts`                    | JSON    | list of contact objects (see model docstring)      |
| `offices`                     | JSON    | list of office objects (see model docstring)       |
| `practice_areas_raw`          | JSON    | source-supplied strings verbatim                   |
| `practice_areas_matched`      | JSON    | list of canonical `PracticeArea.slug` values       |
| `practice_areas_unmatched`    | JSON    | normalized strings that did not match              |
| `additional_data`             | JSON    | escape hatch for source-specific fields            |
| `created_at`, `updated_at`    | dt UTC  | row bookkeeping                                    |

Uniqueness: `(source, source_firm_id)` and `(source, source_url)`. Either
is sufficient for idempotent upsert from a re-scrape.

### `firms` — canonical firm

Best current values for each field, chosen by precedence rules from one
or more source records.

| column                              | type | notes                                       |
|-------------------------------------|------|---------------------------------------------|
| `id`                                | int PK |                                           |
| `name`, `name_normalized`           | str  | best current values                         |
| `website`, `website_normalized`     | str? |                                             |
| `phone`, `phone_normalized`         | str? |                                             |
| `year_founded`                      | int? |                                             |
| `attorney_count`                    | int? | numeric size only; descriptor strings stay in source-record `additional_data` |
| `field_provenance`                  | JSON | see below                                   |
| `created_at`, `updated_at`          | dt UTC |                                           |

`field_provenance` shape:

```json
{
  "name":    {"source_record_id": 17, "source": "az_bar",    "written_at": "2026-05-21T18:14:00Z"},
  "website": {"source_record_id": 41, "source": "justia",    "written_at": "2026-05-21T18:14:00Z"},
  "phone":   {"source_record_id": 17, "source": "az_bar",    "written_at": "2026-05-21T18:14:00Z"}
}
```

Kept as JSON so normal queries on `firms` are unaffected. Writes happen
when resolution assigns a canonical value to a field.

### `firm_source_record_links` — M-to-M

Wires source records to canonical firms. Carries `link_method`
(`auto` / `manual`) and a back-reference to the `match_review_queue`
decision that produced the link (nullable, set NULL on delete).

### `offices` — canonical physical locations

A firm has zero or more canonical offices. Source records hold offices
inline as JSON; resolution promotes them to `offices` rows by matching
addresses across source records mapped to the same firm.

### `persons` + `firm_persons` — attorneys and affiliations

`persons` is the canonical attorney. `firm_persons` is many-to-many,
time-bounded (`start_date`, `end_date`, `active`) — lawyers move.
Strongest identity key is `(bar_state, bar_number)`; falls back to
name+phone+email matching.

`firm_persons` additionally carries:

- `is_primary_contact` (bool) — designates the firm's primary-contact
  slot. Enforced by a partial unique index: at most one primary contact
  per firm. Replacement is governed by `title_rank` (see below) — only
  a higher-ranked candidate replaces the current primary; ties or
  missing rank keep the existing.
- `title_rank` (int, nullable) — assessed seniority derived from
  `title` by the title-rank normalizer (added in M3). Higher is more
  senior. NULL means we couldn't classify the title. See
  `docs/assumptions.md` "Seniority rule for primary contact" for the
  ladder.

### Practice-area taxonomy

`practice_areas` is the canonical taxonomy, populated from
`data/reference/practice_areas.yaml` by a seed script (added in M3).
Three tables work together:

- `practice_areas` — slug, name, `parent_id` (for roll-up), `priority` bool.
- `practice_area_aliases` — alternate strings that map to a canonical
  area. Both `alias` and `alias_normalized` are stored; lookup is on
  `alias_normalized`.
- `firm_practice_areas` — a firm's tagging with a canonical area,
  carrying `source_record_id` for per-tag provenance. A firm/area pair
  may appear once per source.

**Matching policy:** normalize both the scraped value and every alias
the same way (lowercase, trim, collapse whitespace, strip filler words
[`law`, `attorney`, `attorneys`, `lawyer`, `lawyers`, `legal`,
`services`, `practice`], strip punctuation, singularize) and match by
EXACT equality. No fuzzy matching. Unmatched values go to
`unmatched_practice_areas` (DB table) with a count, for periodic review.

**Roll-up:** subcategories carry a `parent_id`. A firm tagged with a
child is *implicitly* tagged with the parent. Roll-up is a query-time
concern — we do not double-write parent rows.

### `unmatched_practice_areas` — review-queue

One row per distinct `normalized_value`. `count` increments on
re-encounter. We periodically review and either add aliases to an
existing area or introduce a new canonical area.

### `match_review_queue` — every resolution decision

Every candidate pair the blocker produces gets a row here, regardless of
outcome:

- `score_total` plus `score_components` JSON (name, phone, website,
  address, people, suffix-difference, …).
- `thresholds` snapshot at decision time, so re-runs can re-decide
  without re-scraping.
- `status` in (`pending`, `approved`, `rejected`, `auto_approved`).
- `(source_record_a_id, source_record_b_id)` is stored in ascending
  order so each unordered pair appears once (check constraint
  `a_id < b_id`).

## Why these shapes (short version)

- **Firm-level source-record grain** — chosen up front for simplicity.
  Person-level sources (state bar) aggregate to firm rows at ingest.
  Splitting contacts/offices into their own source-record tables later
  is a mechanical migration thanks to the stable JSON shapes. See
  `docs/assumptions.md`.
- **Per-field provenance as JSON** — keeps normal queries on `firms`
  fast and lets us re-run precedence rules without altering the main
  table. Alternative (per-field FK columns) bloats the schema with
  every new field.
- **Exact-match-on-normalized for practice areas** — fuzzy matching at
  this granularity produces silent miscategorizations. Misses are
  cheaper than wrong matches because the unmatched log surfaces them
  for review. See `docs/assumptions.md`.
- **Decisions stored with score components and thresholds** — required
  for the principle that "re-running with tuned thresholds doesn't
  require re-scraping."

## Cross-table FK notes

`match_review_queue.resulting_firm_id` and
`firm_source_record_links.match_decision_id` create a reference cycle.
Alembic emits one side as an ALTER on SQLite (via batch mode); both FKs
are `ON DELETE SET NULL` so the cycle is non-blocking at delete time.
