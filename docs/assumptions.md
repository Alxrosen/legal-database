# Assumptions log

A running log of design choices that constrain the system in ways that
might need revisiting. Append, do not rewrite.

Format per entry:

- **Date** — when the assumption was made.
- **Assumption** — the constraint itself.
- **Why** — reasoning, alternatives considered.
- **Trigger to revisit** — what would force a rethink.
- **Enforced where** — code locations / docs.

---

## 2026-05-21 — Source-record grain is firm-level

**Assumption.** A `firm_source_records` row represents a single source's
view of a single firm. Contacts (attorneys) and offices are stored as JSON
lists on the row, not as their own source-record tables.

**Why.** Most useful sources (Justia, FindLaw, firm websites) are
firm-level. State bar directories are person-level; the ingest pipeline
for those will aggregate attorneys into firm rows. One table is simpler
than two, and the JSON shapes are defined cleanly enough that splitting
later is mechanical.

Alternative considered: two parallel source-record tables
(`firm_source_records` + `person_source_records`). Cleaner grain but
twice the surface area. Rejected for v1.

**Trigger to revisit.** (a) We start needing to query individual
attorneys across sources before resolution has run. (b) A source
provides attorney data with structure we want to FK to directly
(e.g. bar number → person). (c) Contact JSON blobs grow large enough to
hurt read performance.

**Enforced where.** `src/legal_sourcing/models/source_record.py`
(`contacts`, `offices` JSON columns and their documented shapes).
`docs/schema.md` "firm_source_records" section.

---

## 2026-05-21 — Per-field provenance is a JSON column on `firms`

**Assumption.** Provenance for which source record supplied each
canonical field is stored in `firms.field_provenance` as JSON, not in a
separate provenance table or as per-field FK columns.

**Why.** Keeps the main `firms` table narrow and normal-query-fast.
Provenance is rarely the subject of queries; when it is, JSON path
queries on SQLite/Postgres are adequate.

Alternative considered: separate `firm_field_provenance` table with
`(firm_id, field_name, source_record_id, value, recorded_at)`. More
queryable but adds joins on every provenance read.

**Trigger to revisit.** We need to answer queries like "which firms got
their phone from Justia?" with index-backed performance, or we want to
keep historical provenance (every change to a field) rather than just
the current source.

**Enforced where.** `src/legal_sourcing/models/firm.py`
(`field_provenance` column).

---

## 2026-05-21 — Practice-area matching is exact-on-normalized

**Assumption.** Source-supplied practice-area strings match canonical
areas only by exact equality on the normalized form of the string. No
fuzzy matching. Normalization rules are:

1. Lowercase.
2. Trim and collapse internal whitespace to single spaces.
3. Strip filler words: `law`, `attorney`, `attorneys`, `lawyer`,
   `lawyers`, `legal`, `services`, `practice`.
4. Strip punctuation.
5. Singularize ("injuries" → "injury").

Both the scraped value and every alias in
`data/reference/practice_areas.yaml` are run through the same pipeline
before comparison.

**Why.** Practice-area strings are short and high-stakes. Fuzzy matching
at this granularity produces silent miscategorizations (e.g. "Family
Law" fuzzy-matched to "Family Mediation"). Misses are cheaper than wrong
matches because we capture them in `unmatched_practice_areas` and review
periodically. The unmatched log is the feedback loop that grows our
alias list.

Alternative considered: rapidfuzz-based matching with a high threshold.
Rejected because errors are silent — we'd only catch them by spot
checks.

**Criteria for adding new canonical categories vs aliases.**
- Add an **alias** when the unmatched string is semantically the same
  practice area, just worded differently ("car crash" → existing
  `auto-accidents`).
- Add a **new canonical area** when the unmatched string describes work
  that doesn't fit any existing category, AND we expect to see it from
  multiple firms (count ≥ ~3 in `unmatched_practice_areas`, or it's
  business-priority).
- New child of an existing parent when the area is a clear specialization
  (e.g. a new sub-area of Personal Injury). Set `priority: true` if it
  rolls up under a priority parent we care about.

**Trigger to revisit.** Unmatched log shows many near-misses that are
unambiguously the same area (e.g. typos, plural forms we haven't
handled). Cost of manually maintaining aliases exceeds the cost of a
careful fuzzy approach with human review on borderline scores.

**Enforced where.**
- Schema: `src/legal_sourcing/models/practice_area.py`.
- Vocabulary: `data/reference/practice_areas.yaml`.
- Normalizer + matcher: `src/legal_sourcing/normalize/practice_areas.py`
  (added in M3).

---

## 2026-05-21 — Unmatched practice areas go to a DB table, not a flat log

**Assumption.** Distinct unmatched normalized strings are stored in the
`unmatched_practice_areas` table with a count, last-seen timestamp, and a
sample source record FK. The original prompt offered "DB table or
data/reference/unmatched_practice_areas.log"; we chose the DB table.

**Why.** Queryable, deduplicated, transactional with the upsert that
discovered the miss, and lives in the same backup/lifecycle as the rest
of the matching state. A flat log would also work but requires extra
plumbing to deduplicate and count.

**Trigger to revisit.** Reviewers prefer editing a text file to running
a query. Easy to add an export script in either direction.

**Enforced where.**
`src/legal_sourcing/models/practice_area.py` (`UnmatchedPracticeArea`).

---

## 2026-05-21 — `practice_areas.yaml` is the source of truth, DB is a projection

**Assumption.** The canonical taxonomy lives in
`data/reference/practice_areas.yaml`. A seed script (M3) upserts the
file's contents into the `practice_areas` and `practice_area_aliases`
tables. Edits happen to the YAML, then we re-seed.

**Why.** YAML is git-versioned, code-reviewable, and easy to diff.
Pushing edits through the DB only would split the source of truth.

**Trigger to revisit.** We outgrow YAML (e.g. need per-area metadata
that doesn't fit cleanly) or want non-engineers editing the taxonomy
through a UI.

**Enforced where.**
- File: `data/reference/practice_areas.yaml`.
- Loader: `src/legal_sourcing/normalize/practice_areas.py` (added in
  M3) — re-runnable upsert.

---

## 2026-05-21 — Storage normalization is "raw + normalized side-by-side"

**Assumption.** Whenever we normalize a value for comparison (name,
phone, website, address, practice area), we keep both the raw original
and the normalized form in the schema. Normalization is for comparison;
storage stays faithful to what the source said.

**Why.** Project-wide principle. We never want to lose what the source
actually wrote — useful for debugging, for human review of merges, and
for re-running normalization with improved rules.

**Trigger to revisit.** Storage cost of duplicate-shaped columns
becomes a real issue (won't happen at pilot scale).

**Enforced where.** Every `*_raw` / `*_normalized` column pair in the
models. `src/legal_sourcing/normalize/` (M3).

---

## 2026-05-28 — Schema additions: `year_founded`, `attorney_count`, `source_last_updated_at`

**Assumption.** Three nullable columns added during M2 schema review:

- `year_founded` (int) — on both `firm_source_records` and `firms`.
- `attorney_count` (int) — on both `firm_source_records` and `firms`.
  Numeric size only.
- `source_last_updated_at` (datetime, UTC) — on `firm_source_records`
  only. The source's own publish / "last updated" date for a firm's
  entry, distinct from `scraped_at` (when *we* fetched the page).
  Canonical `firms` does not carry this — it's a per-source artifact.

**Why.** Year founded is a stable identity-adjacent signal useful for
both display and matching. Attorney count is a numeric size signal we'll
want during resolution and ranking. The source's own freshness date
helps us prefer recent records when sources disagree, separately from
when we happened to scrape.

**Trigger to revisit.** We start needing historical year-founded
disagreement tracking (unlikely — it's a stable fact), or attorney
count needs ranges rather than point values (likely as we hit
self-reported "50+ attorneys" strings — see next entry).

**Enforced where.**
- `src/legal_sourcing/models/source_record.py` (`FirmSourceRecord`).
- `src/legal_sourcing/models/firm.py` (`Firm`).

---

## 2026-05-28 — No firm-size descriptor column; descriptors live in `additional_data`

**Assumption.** Self-reported size strings ("solo practitioner",
"boutique", "AmLaw 100") are NOT promoted to a dedicated column. They
stay in `firm_source_records.additional_data` JSON until we see enough
real source data to decide on a controlled vocabulary or numeric range.
Numeric size lives in `attorney_count`.

**Why.** We don't yet know the actual distribution of how sources
describe size. Building a column now would either be too narrow
(forcing parsers to coerce diverse strings into a small enum) or too
permissive (a free-text column that adds nothing over JSON).

**Trigger to revisit.** Real source data shows ≥3 sources using the
same descriptors AND those descriptors carry meaningful resolution /
ranking signal. At that point, migrate from JSON into a column (or a
small lookup table) with a defined vocabulary.

**Enforced where.** Absence of a column. Documented in
`docs/schema.md`. Migration path: a future Alembic revision plus an
ETL pass that extracts from `additional_data`.

---

## 2026-05-28 — No speculative columns; promote from `additional_data` only after evidence

**Assumption.** Beyond the schema as drafted, we do not add columns in
anticipation of source data. Any source-specific field a parser produces
goes into `firm_source_records.additional_data` JSON. Promotion to a
typed column happens only after we have real parsed data from ≥2
sources showing the field exists, matters for resolution / display, and
has a stable shape.

**Why.** Speculative columns balloon the schema, force parsers to
coerce data they don't have, and either get NULL-everywhere or
silently shaped wrong. Promotion-after-evidence keeps the schema
tightly tied to what we actually see.

**Trigger to revisit.** Process question, not a design question — only
revisit if the "evidence then promote" rule itself becomes onerous
(e.g. parsers grow heavy JSON-shaping logic that a typed column would
simplify).

**Enforced where.** Code review of any new column migration.

---

## 2026-05-28 — `persons` + `firm_persons` are firm-level support, not a primary attorney DB

**Assumption.** The `persons` and `firm_persons` tables exist to
support firm-level use cases — primarily, recording one or a small
number of representative attorneys per canonical firm (managing
partner, key contact, lead litigator) so they can be matched across
sources and surfaced on the firm record.

They are NOT a primary attorney database. We do not plan to:

- Track every attorney at every firm.
- Maintain full attorney histories (CV, education, etc.).
- Build attorney-centric features (per-attorney pages, search-by-attorney
  as a first-class use case).

Per-firm-attorney data on source records lives in
`firm_source_records.contacts` JSON; resolution promotes only the
attorneys we need for firm-level use cases into `persons`.

**Why.** Scope discipline. A primary attorney database is a different
product with different sourcing (every-attorney coverage is much
harder than every-firm coverage), different match heuristics
(name+bar dominates over name+firm), and different storage shape
(career history is time-series-heavy).

**Trigger to revisit.** Product direction shifts toward attorney-level
sourcing as a primary use case. Symptoms: queries against `persons`
exceed queries against `firms`, or we start needing per-attorney
historical tables.

**Enforced where.** `src/legal_sourcing/models/person.py` (current
shape — minimal columns, no history tables). Resolution code (M6+)
should only promote the small set of representative attorneys per
firm.

---

## 2026-05-28 — Practice-area review CLI spec (locked)

**Assumption.** The CLI used to review `unmatched_practice_areas`
(added in M3 alongside the practice-area loader) conforms to the
following spec:

1. **Sort.** Pull entries by `count` descending — highest-impact first.
2. **Display per entry.** Show:
   - The raw and normalized strings.
   - `count` and `last_seen_at`.
   - 3–5 example firms (names + sources) where the string appeared.
   - The current canonical taxonomy for context (slugs + names,
     hierarchical, with priority marked).
3. **Decision options.**
   - `alias of X` — add the normalized string as an alias of an
     existing canonical area X.
   - `new category` — create a new canonical area (prompt for slug,
     name, parent, priority flag, initial aliases).
   - `reject` — record that the string is intentionally not matched
     (e.g. junk values from a source). Persisted so we don't re-prompt.
   - `skip` — no-op. Does NOT mark the row. Restart re-presents it.
4. **Side effects.** When a decision is applied:
   - Insert the appropriate `practice_area_aliases` row (or new
     `practice_areas` row).
   - Backfill `firm_practice_areas` rows for every source record where
     `practice_areas_unmatched` contained this normalized string.
   - Remove the row from `unmatched_practice_areas` (or mark
     `status='rejected'` — TBD when implementing).
5. **Progress.** Show `"X/Y reviewed, Z% of total count covered."` —
   denominator is the sum of `count` across all entries at session
   start, numerator is sum of `count` for entries decided this session.
6. **Undo.** One level. The most recent action can be reverted via
   `u` keypress. Stack depth = 1; a second undo is a no-op.
7. **Suggested default.** Only shown when a best fuzzy-match score
   against existing aliases falls within 5 points of the auto-match
   threshold (i.e. it narrowly missed automatic classification).
   Otherwise no default. **A suggestion is never auto-applied — the
   user must press a key to accept.** This preserves the
   exact-match-only matching policy.

**Why.** This is the human side of the practice-area loop. Locking the
spec up front prevents the CLI from drifting into auto-classification
territory, which would undermine the "exact match only, misses go to
review" decision logged above.

**Trigger to revisit.** The unmatched queue grows faster than humans
can review, even with the one-at-a-time interactive flow. Mitigations
before changing this spec: tighten normalization rules (more filler
words, better singularization), batch-review tooling that proposes
groups, an auto-reject heuristic for clearly-junk strings.

**Enforced where.** `src/legal_sourcing/scripts/review_practice_areas.py`
(added in M3+). Behavior is testable against fixture
`unmatched_practice_areas` rows.

---

## 2026-05-28 — Per-site worker pools and rate limits; global values are fallbacks only

**Assumption.** Scraping is multi-threaded per source. The number of
concurrent workers and the per-second request cap are chosen *per site*
based on the target server's capacity, not from a single global value.

- `RATE_LIMIT_RPS` and `MAX_WORKERS` in `.env` / `Settings` are GLOBAL
  FALLBACKS only — used by ad-hoc scripts and by sources that have not
  declared their own limits yet.
- Each scraper module declares its own `RATE_LIMIT_RPS` and `WORKERS`
  constants (or equivalent class attributes on the base scraper from
  M3). These override the global defaults when that scraper runs.
- Limits should be tuned empirically: start conservative (≤ the global
  default), watch for 429/503/connection-resets, raise until errors
  appear, back off ~30%.
- The base scraper (M3) is responsible for enforcing both the rate
  limit and the concurrency cap, and for honoring `Retry-After`
  headers on 429 responses.

**Why.** A single global RPS either throttles fast-capacity sites
unnecessarily (multi-hour scrapes for no reason) or hammers slow sites
into rate-limiting / IP blocks. Per-site tuning is the only honest
approach. Multi-threading is required to make pilot-scale scrapes
finish in tens of minutes rather than hours.

**Trigger to revisit.** We add a managed crawler service that handles
rate limiting globally, OR a single source becomes large enough that
its concurrency settings need finer control than module-level
constants (e.g. time-of-day variation, dynamic backoff based on
response latency). At that point, lift the per-site limits into a
config table.

**Enforced where.**
- Defaults: `.env.example`, `src/legal_sourcing/config.py` (`Settings`).
- Per-site overrides: each `src/legal_sourcing/scrapers/<source>.py`
  module (added M4+) declares its own constants on top of the base
  class (added M3).

---

## 2026-05-28 — Seniority rule for primary contact

**Assumption.** Each firm has at most one "primary contact" — the
`FirmPerson` row with `is_primary_contact = True` (enforced by a partial
unique index). Population and replacement follow these rules:

1. **All scraped attorneys are kept** in `persons` and `firm_persons`,
   not just the primary contact. The primary slot is a separate concern
   from coverage.
2. **Title rank** for each affiliation is stored on `FirmPerson.title_rank`
   (nullable int, higher = more senior). It is assigned at insert /
   update time by the title-rank normalizer (added in M3).
3. **Initial assignment.** The first attorney we record for a firm
   becomes the primary contact if their rank is known (non-NULL). If
   rank is NULL, the slot is left vacant until a ranked candidate
   arrives.
4. **Replacement.** An incoming candidate replaces the current primary
   contact only when the candidate's `title_rank` is **strictly
   greater** than the current primary's `title_rank`. On equal rank or
   on missing rank (NULL on either side), keep the existing primary.
5. **Removal.** If the current primary's affiliation goes inactive
   (`active = False` or `end_date` set in the past), the slot becomes
   vacant and the next ranked candidate fills it.

**Rank ladder** (initial; tunable as we see real data). Higher is more
senior:

| Rank | Title bucket                                              |
|------|-----------------------------------------------------------|
| 100  | managing partner, managing director                       |
|  90  | senior partner, equity partner, name partner, founder     |
|  80  | partner, principal, shareholder                           |
|  70  | of counsel, senior counsel                                |
|  60  | counsel                                                   |
|  50  | senior associate, senior attorney                         |
|  40  | associate                                                 |
|  30  | junior associate, staff attorney, contract attorney       |
|  20  | attorney (generic), lawyer (generic)                      |
| NULL | unclassified / blank                                      |

Title matching is normalized: lowercase, strip punctuation, collapse
whitespace, strip filler words (`the`, `at`, `for`, `firm`). The
classifier walks the ladder from highest to lowest, returning the
first bucket whose keywords appear in the normalized title. The full
mapping and classifier live in
`src/legal_sourcing/normalize/title_rank.py` (added in M3).

**Why.** Sources differ wildly in attorney coverage (a state bar
lists everyone; Justia lists a curated few; firm websites lead with
managing partners). A first-come-wins rule would make the primary
contact a function of scrape order rather than seniority.
Strictly-greater on rank avoids thrashing the slot on ties.

**Trigger to revisit.** (a) The rank ladder produces obviously wrong
primaries that humans want to override — at that point, add a manual
override flag (e.g. `is_primary_contact_locked`) and respect it. (b)
We start needing >1 contact per firm (e.g. one for litigation, one
for transactional) — at that point promote the bool to a typed
"contact role" enum or a small role table.

**Enforced where.**
- Schema: `src/legal_sourcing/models/person.py`
  (`FirmPerson.is_primary_contact`, `FirmPerson.title_rank`,
  partial unique index `uq_firm_primary_contact`).
- Classifier: `src/legal_sourcing/normalize/title_rank.py` (M3).
- Replacement logic: `src/legal_sourcing/resolution/contact.py` (M6).

---

## 2026-05-28 — Scale target: 160,000 firms

**Assumption.** The pilot scale target is **≥ 160,000 canonical firms**
(up from the earlier 80k working number). With multiple sources per
firm, expect roughly:

- 300k–800k source records total (assumes 2–5 sources per firm; many
  firms appear in only one or two).
- A few million `firm_persons` rows (if we keep all scraped attorneys
  per the seniority assumption above).
- Match-pair candidates pre-blocking: O(N²) is untenable; the blocker
  is the binding constraint.

**Implications already accounted for.**
- Index choices on `*_normalized` columns are real, not decorative —
  blocking joins on them.
- `firm_practice_areas` "one row per source" means a few million
  rows; the FK indexes are there.

**Implications to revisit at M6 (resolution) and M7+.**
- **Blocking strategy.** Multi-key blocking (phone, website domain,
  name prefix + state, normalized address) is required. A naive
  cross-join is ~1.3e10 pairs at 160k — impossible.
- **DB choice.** SQLite handles 160k firms comfortably for batch
  ingestion + offline resolution. Migration to Postgres becomes
  attractive when (a) we need concurrent writers, (b) we want JSONB +
  GIN indexes on the JSON columns, or (c) FTS5 isn't sufficient for
  name-prefix blocking.
- **Memory.** Don't materialize all-pairs in RAM at any point —
  resolution is streaming / chunked over blocks.

**Why the bump.** Updated business target.

**Trigger to revisit.** (a) Target shifts another large factor. (b)
SQLite query latency on `firms` queries exceeds ~1s on the indexed
paths during ingest, indicating it's time for Postgres. (c) Single-
process resolution time exceeds the cycle we want to re-run on
(e.g. nightly).

**Enforced where.** Design-level concern. Tracked here so M6 (blocking)
and M7+ (DB choice) start with the right scale in mind.

---

## 2026-05-28 — robots.txt default is "warn", not hard-block

**Assumption.** `BaseScraper.ROBOTS_POLICY` defaults to `"warn"`:
robots.txt is still fetched and parsed, but a disallowed URL produces
a structured log warning (`scrape.robots_disallowed`) and the fetch
proceeds anyway. Subclasses opt into stricter behavior by setting
`ROBOTS_POLICY = "block"` (raise `RobotsDisallowedError` and skip the
URL) or `"ignore"` (skip the robots fetch entirely).

This reverses the earlier "hard-block default" choice made during the
M3 planning Q&A.

**Why.** Many legal-directory sites have overly broad robots
`Disallow` rules that would block routine, fair-use scraping of
publicly listed firm information. A hard default would make every
new source require an explicit opt-in to scrape at all, which buries
the decision and risks silent zero-result runs. Warning is the
better default: the log makes the policy violation visible without
blocking the work, and we can flip individual scrapers to `"block"`
when (a) the source's ToS / robots are clearly meant to be respected
or (b) we have explicit written permission and want to enforce a
narrower scope.

**Trigger to revisit.** (a) A source sends a takedown / abuse
complaint — at that point flip its scraper to `"block"` (or stop
scraping it). (b) We standardize on a managed crawler that does its
own robots enforcement — at that point the in-process policy
collapses into "ignore" and the crawler is authoritative. (c) Legal
review of the project recommends a stricter posture.

**Enforced where.**
- `src/legal_sourcing/scrapers/base.py`
  (`BaseScraper.ROBOTS_POLICY`, `_fetch_and_store`).
- Per-source overrides: each scraper module's class definition.
- Tests: `tests/test_scraper_base.py`
  (`test_robots_default_warn_proceeds`,
  `test_robots_block_policy_raises`,
  `test_invalid_robots_policy_rejected_at_init`).

---

## 2026-05-28 — AZ Bar: proceed via api-proxy.azbar.org with the documented Password header

**Assumption.** The AZ State Bar scraper targets the JSON API at
`api-proxy.azbar.org` directly (not the HTML front-end at
`www.azbar.org`), authenticating each request with the static `Password`
UUID that the official front-end JS bundle hard-codes. We accept the
legal posture documented below.

**Why this approach.**

- The underlying data — bar number, full name, public address, firm
  affiliation, member status — is information AZ Bar publishes
  intentionally for public consumption. Bulk access does not change the
  per-record privacy posture.
- The HTML site is a thin JS shell over the JSON API; there is no
  meaningful technical difference between rendering 26K pages in a
  browser and calling the API 26K times. Going through the HTML would
  waste compute and bandwidth on both ends.
- The `Password` header is not a per-user credential — it is the same
  UUID baked into the public JavaScript bundle, identifying calls as
  coming from the official site. We accept the risk that AZ Bar may
  treat this as out-of-scope use; if challenged, we stop.

**What we are NOT doing.**

- We do not bypass per-user authentication, paywalls, captchas, or any
  other access control beyond the static header.
- We do not impersonate individual attorneys or use any identifier
  beyond the documented Password header.
- We do not republish, resell, or rebroadcast the raw scraped data.
  Use is internal lead-sourcing only.

**Operational guard rails.**

- Polite ramped rate: **start at 3 RPS with burst capacity 3, linearly
  ramp to 15 RPS over the first 2 minutes**, then steady-state at 15
  RPS. 10 concurrent workers — the RPS gate is the real throttle, so
  workers waiting at the limiter during ramp is acceptable. Exponential
  backoff with `Retry-After` honored.
- `ROBOTS_POLICY = "warn"` (project default). robots.txt at
  `api-proxy.azbar.org` is informational; we log disallows but proceed.
- 401/403 from the API triggers `AZBarApiPasswordRotatedError` and
  aborts the scrape with re-capture instructions — we never retry
  blindly through an auth change.
- Raw payloads gzipped to `data/raw/az_bar/{YYYY-MM-DD}/` (gitignored).
  Reference fixtures committed under `tests/fixtures/az_bar/`.

**Trigger to revisit.**

- AZ Bar (or counsel) tells us to stop. Then we stop, and either pivot
  to firm-website crawls + Justia + FindLaw, or seek explicit
  permission.
- AZ Bar exposes an official bulk endpoint or paid feed — switch to it.
- The Password header rotation cadence becomes a continuous
  maintenance burden (more than once per month).

**Enforced where.**

- Reference doc: `docs/data_sources/az_bar_reference.md`.
- Config: `AZBAR_API_PASSWORD` in `.env`; `Settings.azbar_api_password`
  in `src/legal_sourcing/config.py`.
- Scraper: `src/legal_sourcing/scrapers/az_bar.py`
  (`AZBarScraper`, `AZBarApiPasswordMissingError`,
  `AZBarApiPasswordRotatedError`).
- Recon: `scripts/recon_azbar.py`.

---

## 2026-05-28 — Diagnostic discipline: confirm rotation with evidence, never as a default

**Assumption.** When a request that previously worked starts returning
401/403, **never** conclude the API key / Password has rotated without
explicit evidence. Required evidence: capture a fresh value from
DevTools and confirm the failing call still fails with the new value.
Only then is rotation the cause.

**Why.** In the AZ Bar reconnaissance, the recon script returned 401
and the script's error message blamed Password rotation. The user
correctly pushed back: they verified in an InPrivate browser that the
documented Password was still accepted (200 OK). The real cause was a
**missing `Userid: publictools` header** the original reference doc
hadn't called out as required.

Concluding "rotated" without evidence sent us toward the wrong fix
(asking the user to re-capture a still-valid value) instead of the
right one (diffing our request shape against a working browser
request). The lesson: when a hypothesis is convenient but unverified,
say so loudly and ask for the diff before acting on it.

**Trigger to revisit.** This is process, not design — no real trigger.
Keep the rule.

**Enforced where.**

- `src/legal_sourcing/scrapers/az_bar.py` — the
  `AZBarApiPasswordRotatedError` docstring should reference this entry
  (TODO: update when the parser lands).
- `scripts/recon_azbar.py` — the 401 error message now lists three
  ranked causes (Password rotation, header shape change, endpoint
  moved) instead of jumping to the first.
- `docs/data_sources/az_bar_reference.md` — required-headers section
  explicitly calls out `Userid: publictools` and warns "Missing
  either `Password` OR `Userid` returns 401 with an empty body."
