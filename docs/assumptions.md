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
