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

---

## 2026-06-02 — urlparse must be guarded; pipelines need crash-safe recovery

**Assumption.** No code calls `urllib.parse.urlparse` on scraped /
source-derived data without catching `ValueError`. Use
`legal_sourcing.normalize.url.safe_urlparse`. Additionally, every
source pipeline should support a `load` (parse-from-disk) mode so a
crash in the parse/normalize/upsert tail never forces a re-fetch.

**Why.** Python 3.14 made `urlparse` raise
`ValueError("Invalid IPv6 URL")` on malformed URLs that earlier
Pythons parsed leniently. A full AZ Bar sweep fetched all 35,864
detail pages (~30 min) then crashed in the normalize phase on one
attorney's malformed `FirmURL`, and because fetch+parse+upsert were a
single process with no disk-recovery path, the entire run's DB write
was lost. `scrape_az_bar load` was added to recover from the intact
raw files without re-fetching.

**Trigger to revisit.** A future Python relaxes urlparse again
(unlikely), or we move parsing fully off urlparse. The `load`-mode
gap (only AZ Bar has it; Martindale + FindLaw don't) should be closed
before those sources get full unattended sweeps.

**Enforced where.**
- `src/legal_sourcing/normalize/url.py` (`safe_urlparse`,
  `normalize_url` guard).
- `src/legal_sourcing/parsers/martindale.py`,
  `src/legal_sourcing/parsers/findlaw.py` (scraped-href urlparse
  calls use `safe_urlparse`).
- `src/legal_sourcing/pipelines/scrape_az_bar.py` (`run_load`,
  `_process_and_upsert`).
- Tests: `tests/test_normalize_url.py`
  (`test_normalize_url_never_raises_on_malformed`).

---

## 2026-06-02 — national `full` scrape: city-grain commit + resumable checkpoint

**Assumption.** Martindale and FindLaw national sweeps run in a `full`
mode whose DURABLE UNIT IS ONE CITY. For each state we discover city
slugs from the source's state index page (not a hard-coded city list),
then per city we fetch → parse → normalize → aggregate → upsert and
**commit inside that city's own Session**. Each completed city is
recorded in `data/processed/{source}_full_progress.json`; re-running
the same `full` command skips checkpointed cities, so a multi-day run
survives crashes / Ctrl-C / reboot. National scope = 50 states + DC
(`legal_sourcing.geo.US_STATE_SLUGS`); `--states` narrows it.

**Why.** A national run is multi-day at polite rates and cannot rely
on the process (or an agent session) staying alive. The AZ Bar crash
proved end-of-run upserts lose everything to one bad record;
committing per city bounds the blast radius to a single city and makes
the run idempotently resumable. FindLaw must aggregate the WHOLE city
across practice areas before upserting because
`upsert_firm_source_records` OVERWRITES `practice_areas_*` (does not
union) — the city grain preserves the per-firm practice-area union.

Secondary decisions baked in:
- `full` does the city sweep only; Martindale firm-profile enrichment
  stays in the separate, already-resumable `enrich` mode (folding it
  in would add one fetch per unique firm → hundreds of thousands more
  requests nationally).
- FindLaw discovery drops `all-cities` and `*-county` links (nav /
  aggregation pages that re-list city firms; de-dupe handles overlap).
- Martindale state pages spill the whole metro (DC lists MD/VA towns).
  Harmless (idempotent on name+street), just some redundant fetches.

**Trigger to revisit.** If cross-state metro spillover doubles the
fetch budget unacceptably, dedupe the city universe globally before
sweeping. If a source starts rate-limiting the per-state discovery
fan-out (FindLaw fetches one state-index page per practice area), cache
discovery harder or discover from a single broad practice area.

**Enforced where.**
- `src/legal_sourcing/geo.py` (`US_STATE_SLUGS`, `parse_states_arg`).
- `src/legal_sourcing/pipelines/_checkpoint.py` (`Checkpoint`).
- `src/legal_sourcing/pipelines/scrape_martindale.py`
  (`extract_city_slugs`, `discover_state_cities`, `run_full`,
  `run_load`).
- `src/legal_sourcing/pipelines/scrape_findlaw.py`
  (`extract_city_slugs`, `discover_state`, `run_full`, `run_load`).
- Tests: `tests/test_full_scrape_support.py`.

---

## 2026-06-02 — Justia firm grain = office address; Avvo blocked by Cloudflare

**Assumption.** Justia listing cards are ATTORNEY-level and carry a
lawyer name + office address + phone + practice areas but **no firm
name**. Justia `FirmSourceRecord`s are therefore aggregated by
**normalized office `(street, city)`**: lawyers sharing an office
become one firm-shaped record (`name_raw=""`, those lawyers as
`contacts`, practice areas unioned, `attorney_count` = contact count).
A lawyer with no parseable street is a solo record keyed on the Justia
profile id. Cross-source resolution matches Justia records to named
firms from other sources via phone / website / address (NOT name).

The national Justia sweep iterates states only (no city enumeration);
per-state pagination is **capped** by Justia (beyond the cap a `?page=N`
request is redirected to the page-1 hub with the page param dropped —
the cap signal, since the hub still shows a misleading "Next" link).
Mega-states are thus partially covered at the state grain; deeper
coverage via city / practice-area subdivision (URL builders exist) is a
future lever — same shape as Martindale's 167-page cap.

**Why.** Justia has no firm name to key on, but it does have addresses,
which the resolution layer already blocks on. Office grouping yields
firm-shaped records that resolve cleanly against the named-firm sources.
Profile-page enrichment (one fetch per lawyer) would add firm names but
multiply requests by ~50x — deferred.

**Avvo.** Recon 2026-06-02 found Avvo hard-blocked by a Cloudflare JS
challenge that a full browser header set does NOT pass (unlike Justia).
A working Avvo scraper needs TLS-impersonation (`curl_cffi`) or a
headless browser — i.e. defeating the challenge — which is **not started
without explicit user sign-off**. Avvo scaffolding intentionally NOT
built pending that decision. See docs/data_sources/avvo.md.

**Trigger to revisit.** If firm names become important for Justia
records, add a profile-enrichment pass (mirrors Martindale `enrich`).
If mega-state coverage matters, subdivide by city/practice area. If
Avvo is wanted, pick a transport (curl_cffi vs Playwright) first.

**Enforced where.**
- `src/legal_sourcing/scrapers/justia.py` (browser headers, challenge
  guard, www host).
- `src/legal_sourcing/parsers/justia.py` (`JustiaDirectoryParser`,
  `extract_page_meta`).
- `src/legal_sourcing/pipelines/scrape_justia.py` (`aggregate_by_office`,
  `fetch_state_pages` cap detection, `run_full`, `run_load`).
- Tests: `tests/test_justia_parser.py`.
- Docs: `docs/data_sources/justia.md`, `docs/data_sources/avvo.md`.

---

## 2026-06-02 — pre-resolution data-quality guards (DB audit findings)

A DB audit before canonical resolution surfaced several "gotchas" that
would corrupt blocking/scoring. Guards added:

**Aggregator/social websites are not match keys.** Beyond a source's
own domain, third-party directories (`lawfirms.com`, `avvo.com`,
`lawyers.com`, `superlawyers.com`, ...) and social/maps domains
(`facebook.com`, `linkedin.com`, `google.com`, ...) collapse — via
`normalize_url` — to one shared domain, so they'd fabricate website
matches across unrelated firms. `normalize.url.is_aggregator_domain`
+ `AGGREGATOR_DOMAINS`; `normalize_record` nulls `website_normalized`
when it hits one. Revisit the list as new aggregators show up.

**Martindale office state backfilled from the URL.** Martindale SRP
cards show only the city; the state is implied by the
`/all-lawyers/{city}/{state}/` URL. Without backfill ~94% of Martindale
firms had no state, breaking `primary_state`, the `name_state` blocking
key, and state scoring. `geo.STATE_SLUG_TO_ABBR` +
`scrape_martindale._backfill_office_state` fill it pre-normalize.

**Still operational (NOT code), required before resolution:**
`primary_city/state/postal_code` are 0 in the DB — they live in the
`offices` JSON but the columns aren't derived. The re-derive step
(scripts/renormalize_addresses.py / task #48) MUST run after scraping,
and a re-normalize/`load` pass is needed for the website + state guards
to take effect on already-fetched rows (no rescrape). Martindale
`enrich` should also run (full mode skips firm profiles, leaving
Martindale with almost no website/phone).

**OPEN DECISION — government/court entities.** AZ Bar (and others)
include attorneys at courts / AG offices / public defenders / agencies
("maricopa county superior court" = 30 attorneys, 299 `.gov` sites).
These aren't acquisition targets and form large false clusters. Whether
to EXCLUDE or just FLAG them in the canonical set is a product decision,
deferred to the user. Not yet implemented.

**Enforced where.**
- `src/legal_sourcing/normalize/url.py` (`AGGREGATOR_DOMAINS`,
  `is_aggregator_domain`).
- `src/legal_sourcing/geo.py` (`STATE_SLUG_TO_ABBR`).
- `src/legal_sourcing/pipelines/scrape_az_bar.py` (`normalize_record`).
- `src/legal_sourcing/pipelines/scrape_martindale.py`
  (`_backfill_office_state`).
- Tests: `tests/test_resolution_data_quality.py`.

---

## 2026-06-02 — website enrichment tracked separately, keyed by website (PROPOSED)

**Context.** We will enrich firms from their OWN websites (attorney
count, description, offices, etc. — see
docs/data_sources/firm_websites.md). This must NOT collide with
Martindale *profile* enrichment (`FirmSourceRecord.enrichment_status`),
and it must be idempotently re-runnable as new firms/websites appear
(new directory scrapers; Martindale `enrich` adding websites later).

**Decision (proposed, confirm before building).** Track website
enrichment in a **dedicated table keyed by normalized website**, NOT a
per-`FirmSourceRecord` column.

Why a website-keyed table (idiomatic + less complex, not more):
- One website maps to MANY source records (`forthepeople.com` -> 464)
  and eventually one canonical Firm. Crawl ONCE per unique site, store
  once, reference many — a per-record `website_enriched_at` column would
  re-crawl per record or duplicate the result across 464 rows, and
  overloads `FirmSourceRecord`.
- "What to enrich next" is a clean set-difference:
  `SELECT DISTINCT website_normalized FROM firm_source_records
   WHERE <valid website> AND website_normalized NOT IN
   (SELECT website FROM website_enrichment WHERE enriched_at IS NOT NULL
    AND enriched_at > <staleness cutoff>)`.
  This auto-handles: (a) new firms/sources on an already-enriched site
  -> skipped; (b) Martindale `enrich` adding NEW websites -> picked up.
  Absent row / NULL `enriched_at` = never enriched (or no website).
- Resolution-agnostic (keyed by the website string); when canonical
  Firms exist they inherit enrichment via their members' websites.

Proposed columns (subset of firm_websites.md §7): `website` (PK,
normalized), `resolved_url`, `platform`, `about_page_url`,
`attorney_count_min`, `attorney_count_is_min`, `attorney_count_raw`,
`attorney_count_method`, `staff_count_min`, `office_count`,
`office_addresses` (JSON), `years_in_operation_min`, `description_blurb`,
`description_generated`, `scope`, `notable_signals` (JSON),
`phones` (JSON), `url_verification_status`
(`verified`/`legal_but_mismatched`/`not_a_law_firm`/`unreachable`),
`url_verification_score`, `fetched_at`, `enriched_at`, `raw_html_path`.

**Overwrite policy.** Website-derived `attorney_count` is the most
authoritative source and may overwrite the directory count on the
canonical Firm (post-resolution), keeping the original + provenance.
Store snapshot date; never overwrite a newer figure with an older fetch.

**Extraction posture.** Hybrid (Alex's call, for cost): heuristic
cascade for count/offices/years + the legal-relevance gate; LLM only for
the generated description and genuinely ambiguous counts. Rules in
docs/data_sources/firm_websites.md §12.

**Alternative considered + rejected:** a `website_enriched_at` timestamp
column directly on `FirmSourceRecord` — simpler to add but re-crawls per
record / duplicates results and overloads the row.

**Enforced where (to build):** `models/website_enrichment.py` + Alembic
migration; `pipelines/enrich_websites.py`; scraper for fetching firm
pages (may reuse `BaseScraper`). This entry is the design of record.

---

## 2026-06-02 — canonical precedence redesign (CONFIRMED by Alex, from sample-merge analysis)

Read-only sample-merge over real website/phone clusters showed the
current `apply.py` rules are wrong for big firms:

| firm (website) | records (by source) | current attorney_count (MAX) | distinct attorneys observed | website reality |
|---|---|---|---|---|
| Kutak Rock | 104 (3 az, 101 justia) | **29** | 196 | ~600 |
| Morgan & Morgan | 464 (2 az, 450 findlaw, 12 justia) | **5** | ~390 | ~1000+ |
| Holland & Hart | 57 (6 az, 51 justia) | **20** | 142 | ~500 |
| Goetz | 1 (az) | 1 | 1 | 40 |

**Validated changes (Alex's spec):**
- **`attorney_count` = website-stated if present, else COUNT of DISTINCT
  attorneys** across the cluster — NOT `MAX` (MAX wildly undercounts).
- **Union multi-valued fields** (practice areas, phones, offices,
  notable signals). **Phones: union, keep ALL, dedup exact only** (never
  discard).
- **Precedence tiers for scalar fields** (firm name, description, year):
  1. website enrichment (the firm's own site) — TOP
  2. Martindale ENRICHED / sponsored profiles (rich)
  3. other directory cards (FindLaw firm-level, Martindale card)
  4. state bar assns (AZ Bar + ~49 future) + Justia — BOTTOM
     (Justia used mainly to SOURCE firm websites to scrape).
- A **running registry of scraped websites** (the website-keyed
  `website_enrichment` table) replaces any hand-maintained Justia list.

**Pitfalls found (need resolution before building — see chat):**
1. **Attorneys live in different fields per source**: justia/az_bar in
   `contacts`; FindLaw person-level records in `name_raw` (contacts
   empty → mijs.com showed 0 from contacts despite 44 attorneys). The
   distinct-attorney count must union `contacts` names AND person-level
   `name_raw`. Cross-source dedup is by normalized name only (no unique
   ID) → approximate.
2. **Firm-name hazard**: FindLaw `name_raw` is often a PERSON (Morgan &
   Morgan = 390 person-named findlaw records), Justia `name_raw` is
   empty. All-justia / all-findlaw clusters have NO firm name until
   website enrichment supplies it. Firm name must come from
   website / Martindale-enriched / az_bar Company — never a FindLaw
   person-name.
3. **Lead-gen phones**: a phone shared across MANY distinct firm
   websites = lead-gen → must NOT merge on it (M&M's line spans 1 site =
   safe; a toll-free across many sites = unsafe).

**Decisions (CONFIRMED by Alex 2026-06-02 — written down to revisit if any prove material):**
1. **attorney_count** = website-stated if present, else COUNT of
   distinct attorneys (union of `contacts` names + person-level
   `name_raw`), accepting imperfect NAME-ONLY cross-source dedup
   (no unique attorney ID). NEVER `MAX`.
   *Revisit if* name-only dedup (variant spellings / collisions) visibly
   distorts counts -> add bar-id / email-based dedup.
2. **Grain**: multi-office national firms collapse to ONE canonical firm
   carrying the website's national count (the firm is the entity, not
   the office). *Revisit if* per-office granularity is needed.
3. **Lead-gen phone guard**: do NOT merge on a phone ONLY when it is
   **toll-free** (NANP 800/833/844/855/866/877/888, i.e. `+1 8xx` with
   those exchange digits) AND it spans > 3 distinct firm websites. Local
   numbers are NOT suppressed. *Revisit/extend* if other lead-gen
   indicators surface (shared marketing-vendor lines, etc.).
4. **FindLaw person-name records** (Morgan & Morgan = 450 person-named
   findlaw rows; mijs.com = 44) are handled via PRECEDENCE for now (firm
   name comes from website / Martindale-enriched / az_bar Company, never
   a FindLaw person-name; their attorneys still feed the distinct count).
   **MARKED FOR LATER (not yet investigated):** confirm whether the
   FindLaw parser is mis-emitting per-attorney records as firms, or
   whether M&M floods FindLaw with per-attorney profiles — affects both
   the count and the name. Flagged here so we can circle back.

**Field-precedence note:** `name`/`name_normalized` needs a FIELD
override (`FIELD_PRECEDENCE_OVERRIDES`) ranking firm-name-bearing
sources (Martindale-enriched, then az_bar `Company`) ABOVE FindLaw
(person-names) and Justia (empty). `justia` is still to be ADDED to
`DEFAULT_SOURCE_PRIORITY` (bottom tier, with the state bars).

**To build (bundled with the website_enrichment table + a first
data-ready canonical draft, so the website TOP tier is included rather
than retrofitted):** `apply.py` precedence tiers + distinct-attorney
count + phone-union; `blocking.py` toll-free lead-gen guard; likely a
`firms.phones` JSON column (the union list) + migration.

---

## 2026-06-03 — canonical resolution = ITERATIVE TRUTH DISCOVERY (idiomatic; REVISED, CONFIRMED by Alex)

**REVISION.** This supersedes an earlier same-day note that mandated a
*deterministic* argmax-by-fixed-confidence. Alex relaxed the determinism
requirement in favor of the idiomatic method: *"Resolution does not have to be
deterministic, please follow the idiomatic option... do research and, if there
is a clear idiomatic way of doing things, suggest that first."* Plus: *use the
time it was fetched when computing the confidence score, iteratively.*

**Assumption.** Cross-source scalar conflicts (firm name, description, year,
attorney_count) are resolved by **iterative truth discovery / data fusion**
(TruthFinder / EM-style), which JOINTLY estimates per-(firm, field) truth and
per-**source reliability** `r_s` — rather than a hand-fixed tier order.

1. Each cluster member contributes claims `(field, value, source, fetched_at)`,
   where **`source` is the fusion key = origin + ENRICHMENT LEVEL**, not the
   bare origin (CONFIRMED 2026-06-03). Reliability is a property of the source,
   and an *enriched* record is a different-quality source than its bare card:
   - `website` — a firm's own site (`website_enrichment` rows); distinct source.
   - `martindale_enriched` (FirmSourceRecord `enrichment_status='enriched'`) vs
     `martindale_card` (card only) — split, NOT one "martindale".
   - `findlaw`, `az_bar` / `{st}_bar`, `justia` as-is.
   Derived at apply-time from `enrichment_status` + the `website_enrichment`
   table — no schema change. (Mirrors the website case, where extraction method
   already sub-weights the website source; same idea applied to Martindale.)
2. `r_s ∈ (0,1)` initialized from priors (website 0.90 · martindale_enriched
   0.75 · findlaw / martindale_card 0.50 · state bars 0.40 · justia 0.35); the
   website's per-method quality (stated > profile-links > solo > heading-roles)
   further scales its claim weight.
3. **Iterate to convergence:** *truth step* — per (firm, field) truth = argmax
   over values of `Σ_{sources asserting it} r_s · recency(fetched_at)`;
   *reliability step* — re-estimate `r_s` from agreement with the current truths.
4. **recency(fetched_at) = exp(-λ·age)** down-weights stale fetches (the
   temporal / "evolving truth" variant) — so a fresh scrape with new info can
   move the truth and an old fetch decays.
5. **Union FIRST for multi-valued fields** (phones, offices, practice areas,
   notable signals): every source contributes regardless of `r_s`; each element
   keeps its support weight as confidence. The distinct-attorney count is the
   UNION of all sources' attorneys (low-reliability sources still add attorneys).

**NOT deterministic across data changes (intentional, CONFIRMED).** Adding
sources/firms re-estimates `r_s` globally, so a re-run can shift records as the
system *learns* which sources to trust — that is the point of truth discovery,
and it lets recency matter. (Within one run with fixed init it is reproducible.)
The earlier "must be deterministic / no fetch-time" constraint was **dropped**.

**Representation.** Per-field truth + confidence (normalized winning weight)
recorded in `firms.field_provenance` JSON; learned `r_s` persisted for audit.
Confidence/`r_s` derived at apply-time from stored signals + `fetched_at` — no
new schema column needed.

**Trigger to revisit.** If global re-estimation churns canonical records too
much between runs, freeze `r_s` (compute once, snapshot) for stability; if
clusters are mostly 1–2 sources (little to fuse), a single reliability-weighted
vote (one iteration) suffices.

**Enforced where (to build at canonical-apply time):** `resolution/apply.py`
(the truth-discovery fusion replaces the per-field `_pick`); `field_provenance`
carries truth + confidence; `r_s` persisted. The website pilot only needs to
CAPTURE claims (value + source + extraction method + `fetched_at`). Refs:
docs/data_sources/firm_websites.md §12; arxiv.org/abs/1503.00310 (Dong &
Srivastava, *Data Fusion*); TruthFinder; incremental/evolving-truth discovery.

---

## 2026-06-03 — concurrent-scrape durability: fetch-to-disk + in-memory buffer + bulk upsert

**Assumption.** Multiple scrapers (Martindale full, the website enricher, future
state bars) run concurrently against the one SQLite DB without slow-lock /
"database is locked" failures AND without losing hours on a crash, via three
rules:

1. **Raw bytes hit DISK immediately, per fetch** (gzipped under `data/raw/...`).
   This is the expensive, network-bound artifact and the no-re-scrape backstop;
   a crash loses at most the one in-flight page.
2. **Extracted rows buffer IN MEMORY, then bulk-upsert periodically** (~50 rows
   or ~60s) via SQLite `INSERT … ON CONFLICT DO UPDATE`. Bulk commits minimize
   write-lock acquisitions so concurrent scrapers interleave cleanly. A crash
   loses only the un-flushed extracted rows — recoverable by `load` mode
   (re-parse from the on-disk raw, NO re-fetch), so minutes of cheap re-extract,
   never hours of re-fetch.
3. **One DB writer per process.** Worker threads only fetch + extract and hand
   results back; a single committer does all writes. Concurrent *writers* stay
   at ~2 (Martindale + enricher) — comfortable for WAL — regardless of fetch
   worker count.

This is on top of the existing **WAL mode + 30s busy-timeout + commit-retry**
(rollback + re-apply, 6× backoff) in `upsert_firm_source_records`.

**Why in-memory (not a disk spool) for the buffer.** Alex suggested a temp
store "ideally not disk." The in-memory buffer is safe *precisely because* the
raw bytes are already on disk: the buffer only holds cheap-to-recompute
extracted rows. A disk spool would be needed only if we did NOT persist raw.

**Trigger to revisit.** Migrate to Postgres (then real concurrent writers +
`COPY`/upsert remove the single-writer constraint), or a single process needs
multiple writer threads (then a queue + one committer thread, same idea).

**Enforced where.** `pipelines/enrich_websites.py` (buffer + bulk
`on_conflict_do_update`); `scrapers/base.py` (raw-to-disk per fetch);
`pipelines/scrape_az_bar.upsert_firm_source_records` (WAL + retry).

---

## 2026-06-04 — Multi-agent shared database: worktree isolation + one WAL DB + central engine factory

**Assumption.** Several Claude sessions work the repo in parallel, each in its
own **git worktree on its own branch** (`Mastermind` = coordinator/integration,
`Websites` = enrichment hardening, `Canonizer` = canonical resolution). They do
NOT share a working tree (a tree has one checked-out branch). They DO share one
database: every worktree points `DB_PATH` (+ `RAW_DATA_DIR`/`PROCESSED_DATA_DIR`)
at the single live `data/legal_sourcing.sqlite` in the main checkout, via an
absolute path in its own (gitignored) `.env`.

Multiple processes reading/writing that one DB is safe given three rules:

1. **WAL + uniform 30s busy_timeout on every connection.** WAL allows many
   concurrent readers + one writer; writers serialize but do not corrupt. The
   `busy_timeout` is the single most important setting — without it a connection
   that loses the writer race fails *instantly* with "database is locked"
   instead of waiting. It was previously set only in martindale/justia/findlaw;
   resolution/*, enrich_websites, az_bar and scrape_state_bar fell back to
   sqlite's ~5 s default. A central engine factory now applies it everywhere.
2. **Agents write DISJOINT tables.** Martindale → `firm_source_records`;
   Websites → `website_enrichment`; Canonizer → `firms` /
   `firm_source_record_links` / `match_review_queue`. No two writers touch the
   same table, so serialization is just a short queue, never a logical conflict.
   (Builds on the 2026-06-03 single-writer-per-process + bulk-upsert entry.)
3. **Migrations are Mastermind-only and run only when scrapes are quiesced.**
   Alembic DDL takes heavy locks / rewrites tables — the one operation genuinely
   unsafe against a live writer. The coordinator owns the schema.

**Why this over alternatives.** Per-agent DB snapshots (copy, work isolated,
merge back) give maximum isolation but go stale against the live Martindale
scrape and need manual merge-back; rejected as the default (kept as an option
for the Canonizer if it wants a reproducible read, since `firms`/links/queue are
empty so its output is a clean insert). Postgres is the textbook multi-writer
answer but is a heavy migration we don't need while writers are table-disjoint.
The relative default `db_path` (`./data/...`, resolved against CWD) is a footgun
across worktrees — each would silently open its own empty DB — so the
absolute-`DB_PATH`-per-worktree rule is mandatory. Verified end-to-end: a
`make_engine()` connection from the `Mastermind` worktree reports the absolute
main-tree `db_url`, `busy_timeout=30000`, `journal_mode=wal`, and sees the live
row counts (236k+ source records, the Websites session's `website_enrichment`
rows).

**Trigger to revisit.** (a) Two agents genuinely need to write the *same* table
concurrently → move to Postgres (MVCC, row locks). (b) Migration coordination
becomes a bottleneck (frequent schema churn during long scrapes). (c) A worktree
needs to run a scrape's `load` against raw it didn't fetch → its
`RAW_DATA_DIR`/`PROCESSED_DATA_DIR` already point at the shared dirs.

**Enforced where.** `src/legal_sourcing/db.py` (`make_engine` + connect-time
PRAGMA listener); `resolution/{run,apply}.py` and
`pipelines/{scrape_az_bar,scrape_state_bar,scrape_martindale,scrape_justia,scrape_findlaw}.py`
(routed through `make_engine`); each worktree's `.env` (absolute shared paths);
`config.py` (`db_path` is env-overridable). `enrich_websites.py` to be routed by
the Websites session.

---

## 2026-06-04 — Canonical resolution: truth-discovery fusion + strong-identifier matching

**Assumption.** A canonical `Firm` is built field-by-field from its cluster of
source records by a **reliability- and recency-weighted vote** (data-fusion /
truth-discovery survivorship), NOT by fixed source precedence. Per-field rules:

* **name** — gate to firm-like names (`identity.is_firm_name`, excludes empty
  Justia names and person names), fuzzy-group near-duplicate variants by
  normalized form, weighted-vote the groups, emit the most-supported raw surface
  form. A lone mis-attributed name ("Arizona Supreme Court" on swlaw.com) lands
  in its own low-weight group and loses. Falls back to a person name only if the
  cluster has no firm-like name (solo practitioners).
* **phone / website** — weighted vote on the exact value; aggregator/social/
  website-builder domains (`identity.is_identity_website`) are never a firm
  website. Multi-office firms surface the most-claimed line.
* **attorney_count** — a VERIFIED website headcount is authoritative (the firm's
  own statement); otherwise the distinct-attorney union across the cluster,
  flagged `is_min` (we only count attorneys we scraped — a lower bound).
* **year_founded** — vote of source-supplied years; else derived (approximate)
  from a verified website's years-in-operation.

Each pick records provenance (source record, method, support, confidence,
`is_min`/`approximate` flags) in `firms.field_provenance` (free-form JSON — no
schema change). Source reliability uses per-origin priors
(martindale > findlaw > az_bar > justia, +bonus for enriched) × a gentle recency
decay; frequency + completeness usually dominate, priors break ties.

**Matching (who clusters).** Pairwise scoring keeps the weighted-sum components
but adds: (1) a **missing name is neutral** (None), not a 0 penalty — Justia has
no firm name; (2) **website-identity match floors the score into the auto-merge
band** (a shared non-aggregator domain is, in this corpus, a one-firm signal);
(3) **phone-match + strong-name-match also floors** — the only strong signal for
the ~83% of records with no website (the name requirement makes shared
lead-gen / toll-free numbers safe); (4) **caps win over floors**: clearly-
different firm names, or two different identity websites, cap the pair out of the
auto band for human review.

**Why (evidence).** Real clusters broke fixed precedence: Snell & Wilmer (38
records) shattered into 38 singletons at threshold 85 because office phones
differ, `primary_state` is empty, and missing names scored 0; the old `_pick`
also kept only one arbitrary record per source. The weighted vote + website
floor consolidates it to one firm (name beats 5 variants + an outlier, headcount
500 from verified enrichment). The phone+name floor consolidates websiteless
multi-office firms (Frank Azar's 11 offices) while a toll-free lead-gen line's
distinct solos correctly stay separate. `looks_like_firm`'s `" pa"` marker
substring-matched surnames ("Parker", "Patrick"); `identity.is_firm_name` fixes
that for resolution.

**Trigger to revisit.** (a) A non-aggregator domain turns out to be shared by
genuinely different firms at scale (extend `PLATFORM_DOMAINS`/aggregator list, or
add a "domain shared by N distinct firm names" guard). (b) We want a *global*
TruthFinder pass that LEARNS source reliability from inter-source agreement
(current priors are fixed). (c) The `Firm` schema gains columns (offices,
practice areas, description, scope) — fusion already computes some and can
populate them once a migration lands (Mastermind owns migrations).
(d) `primary_state` backfill (Mastermind) enables the name+city+state path, which
should then also floor.

**Enforced where.** `resolution/fusion.py` (field-by-field vote + `fuse_cluster`),
`resolution/identity.py` (`is_identity_website`, `is_firm_name`,
`PLATFORM_DOMAINS`), `resolution/scoring.py` (floors/caps + neutral-missing-name
+ identity-website gate), `resolution/blocking.py` (identity-website keys only),
`resolution/apply.py` (union-find → `fuse_cluster` → `firms`/links +
`field_provenance`). `resolution/sample_eval.py` is the read-only harness used to
validate clusters without writing the canonical tables.
