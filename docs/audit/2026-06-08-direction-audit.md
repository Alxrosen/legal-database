# Direction audit — `legal-deal-sourcing`

**Date:** 2026-06-08 · **Author:** Cleanser (project auditor) · **Scope:** high-level
architecture & direction, not line-by-line implementation.
**Method:** 7 parallel read-only auditors (one per dimension), each evidence-based and
idiomatic-first, synthesized here. Grounded in the live DB, the import graph, and the docs
as of `main` @ `8c2b5ef`.

## Bottom line

The project is **fundamentally sound and unusually well-built** — a real (not aspirational)
6-layer separation, strict scrape/parse isolation, a research-aware resolution design, and a
decision-capture discipline (`assumptions.md` / `COORDINATION.md` / handoff docs) better than
most production teams have. **No dimension is `needs-rethink`.** The risks are not in *what is
built* but in three gaps the project is about to grow into:

1. It is **tuning resolution by eyeball** (no labeled eval set, no precision/recall) at a scale
   (~406k records, +49 state bars pending) where hand-tuned constants won't be known to generalize.
2. The **canonical record doesn't yet meet the business goal** — the EBITDA-proxy signals that
   define a "deal target" live in side tables, and there is no scoring/ranking/shortlist step.
3. A just-made (correct) decision — **website becomes a first-class source** in
   `firm_source_records` — puts a second writer on the hottest table, which trips the project's
   own logged trigger to move to **Postgres**.

## Scoreboard

| Dimension | Verdict |
|---|---|
| Scraping & ingestion | ✅ on-track |
| Package architecture & layering | 🟡 minor-adjustments |
| Data model & schema | 🟡 minor-adjustments |
| Entity resolution & fusion *(core bet)* | 🟡 minor-adjustments |
| Multi-agent workflow & shared DB | 🟡 minor-adjustments |
| Tooling, testing & CI | 🟡 minor-adjustments |
| Docs & direction coherence | 🟡 minor-adjustments |

## Strengths (protect these)

- **The 6-layer architecture is real** — verified clean downward DAG, **zero circular imports
  across 58 modules**, single choke points (`db.make_engine`, `config.get_settings`,
  `utils.logging`). The "scrape vs parse" split the docs promise is structurally true.
- **Ingestion is battle-tested** — immutable gzip raw + JSON sidecar, `load`-from-disk re-parse
  (no re-fetch), per-unit atomic checkpoints (born from the real AZ Bar 35,864-page loss),
  evidence-tuned politeness + Cloudflare handling. Hand-rolling httpx over Scrapy is the *right*
  call for this finite, paginated-directory workload — not reinvention.
- **Resolution is research-aware** — correctly decomposed into the standard ER stages
  (block → compare → classify → cluster → canonicalize), cites the right literature
  (Fellegi-Sunter, TruthFinder, Bleiholder & Naumann, MDM survivorship), and **every floor/cap
  traces to a concrete real-cluster failure**. The score-vs-policy split (`match_review_queue`
  stores `score_components` + a thresholds snapshot, re-decidable without re-scoring) is mature.
- **Decision capture is the standout asset** — dated, append-only `assumptions.md`
  (date / assumption / why / trigger-to-revisit / enforced-where) + git-as-transport coordination.
  This is what makes the multi-agent setup survive compactions.
- **Indexes are real, not decorative** — the columns the resolver blocks/joins on
  (`phone_normalized`, `website_normalized`, `name_normalized` + `primary_state`, FK columns)
  are all indexed.

## High-severity findings (the few that matter)

### P1 — There is no measurement *(resolution)*
The entire system is validated by manual `sample_eval` dry-runs over ~20 firms. No labeled pair
set, no precision/recall, no confusion matrix. "All failures are under-merges" is **asserted, not
measured**. Thresholds (85/60/40) and override constants (88/86/86/55/50) are tuned by eyeballing
clusters.
- **Idiomatic move (build FIRST — engine-agnostic, lowest-regret):** hand-label a few hundred
  candidate pairs stratified across blocking keys + score bands; add a scored eval command
  (**extend `sample_eval`, don't write a throwaway**) reporting precision / recall / pairwise-F1
  (and ideally B-cubed). This gates principled tuning *and* any engine decision below.
- **Owner:** Canonizer · **Effort:** medium

### P2 — The match layer reinvents Splink, and the bespoke part is the brittle part *(resolution)*
The decision is a hand-tuned weighted-additive score with a *growing* stack of hard floors/caps +
a bespoke `_UnionFind`. That is a hand-approximation of Fellegi-Sunter. The override stack has
grown once per discovered failure, and the trend is *more* bespoke rules (the 2026-06-08 entry
already anticipates a multi-domain size-gate).
- **Idiomatic move:** **Splink** (MIT, moj-analytical-services) — Fellegi-Sunter with
  **unsupervised EM-learned** m/u match weights, native blocking rules, DuckDB backend
  (~1M records/min on a laptop), built-in connected-components clustering. A near-exact superset of
  `blocking.py` + scoring weights + `_UnionFind`. Pilot it against the P1 labeled set and the
  known-firm oracle (Snell & Wilmer, Morgan & Morgan, the multi-domain cases); adopt if it
  matches/beats the override stack. Keep `rapidfuzz` for fusion-layer name-variant grouping.
- **Owner:** Canonizer (resolution domain) · **Effort:** high · *(full plan: forthcoming)*

### P3 — The canonical record doesn't meet the business goal *(docs_direction + data_model)*
The purpose is *finding deal targets*. The project correctly extracts EBITDA-proxy signals
(headcount, `office_count`, `scope`, years, `notable_signals`) — but they live only on
`website_enrichment` (domain-keyed) and source records. The canonical `firms` table is
**scalar-only** (name/phone/website/year/count); `fuse_cluster` even computes union fields
(offices, phones) that **have nowhere to land**, and the canonical `offices`/`firm_persons`/
`firm_practice_areas` tables exist but are **empty** (apply.py writes only Firm + links). There is
**no scoring/ranking/shortlist step at all** (grep confirms). As built, after fusion you get a
clean directory that answers "who is this firm," not "which firms are good targets" — and that gap
is currently **nobody's lane**.
- **Idiomatic move:** (a) promote the deal-signals onto canonical `Firm` + populate the existing
  child tables at survivorship time (the migration `assumptions.md` 2026-06-04 already anticipates;
  fold it into the website-as-source widening so it happens once); (b) add an explicit deal-target
  scoring step — a `firms_scored` SQL view or `scripts/rank_targets.py` storing score components
  (mirroring `match_review_queue`); (c) one `assumptions.md` entry defining "good deal target."
- **Owner:** Mastermind to assign (currently unowned) · **Effort:** medium

### P4 — Resolution recall is blocked right now *(data_model)*
`primary_city` / `primary_state` are **NULL on 100% of 406,020 rows** — indexed columns wired to a
deriver (`backfill_primary_address`) that hasn't run. This silently kills one of three blocking
keys and three scoring floors; name+city+state is the *only* signal for the ~83% of records with
no website. **Known and queued** (Mastermind runs it post-Martindale-scrape).
- **Idiomatic hardening:** make it a **STORED generated column** off the `offices` JSON so it
  can't go stale; or at minimum a release-blocking assertion that `primary_state` is populated
  before any `apply` run. Indexed-but-always-NULL is the state to eliminate.
- **Owner:** Mastermind (backfill queued) · **Effort:** medium

### P5 — Website-as-a-source puts Postgres on the critical path *(multiagent_db)*
The (correct, idiomatic MDM) decision to emit website as `source="website"` in
`firm_source_records` means **two writers now share the hottest table** — defeating the
disjoint-lane invariant that makes WAL safe. It is rescued *only* by manual post-scrape sequencing.
This trips the project's own logged Postgres trigger ("two agents need to write the same table").
- **Idiomatic move:** keep SQLite through the current scrape + first apply; treat the
  website-as-source cutover as the **Postgres migration point** (MVCC, row locks, JSONB/GIN for the
  heavy JSON columns, online DDL, per-role write GRANTs that make lanes *DB-enforced* instead of
  honor-system). `make_engine` already abstracts the swap.
- **Owner:** Mastermind (owns the cutover) · **Effort:** high · *(full plan: forthcoming)*

### P6 — No CI on a repo where 4 agents merge to shared `main` *(tooling)*
The 294-test suite runs in **4.5s** and `make check` is CI-ready — yet nothing enforces it before a
merge. For parallel agents on one codebase + one DB, a silent regression on `main` breaks the other
sessions.
- **Idiomatic move:** `.github/workflows/ci.yml` using `astral-sh/setup-uv` → `uv sync --extra dev`
  → `make check` + `make test`; branch protection requiring it. Near-instant, highest-value
  tooling change.
- **Owner:** Cleanser (proposed) · **Effort:** low

## Notable medium/low items (condensed)

- **architecture:** the source-agnostic spine (`normalize_record` / `aggregate_by_firm` /
  `upsert_firm_source_records`) lives *inside* `pipelines/scrape_az_bar.py` and is imported
  sideways by 4 sibling pipelines → extract to `pipelines/source_record.py` (leave a re-export
  shim). `is_firm_name` is forked from the buggy `normalize.name.looks_like_firm` → fix at source.
- **data_model:** `field_provenance`-as-JSON is fine now but is becoming the audit trail for a
  non-deterministic fusion; set a revisit trigger to a structured `firm_field_provenance` table if
  audit queries become routine. Four divergent office-address shapes across the schema.
- **resolution:** `fuse_attorney_count = max(verified_website, distinct_union)` is non-robust
  (0% breakdown point — one inflated parse wins) → union as a hard floor + corroboration gate
  (scope / office_count / cluster size) + median/trimmed-mean once multiple estimates exist.
  *(Already Canonizer's OPEN ITEM 1.)* Source reliability priors are fixed, not learned — Splink's
  m/u weights would supply this for free.
- **ingestion:** `aggregate_by_firm` uses `id(r)` as a fallback group key for nameless records →
  non-deterministic `source_firm_id`, breaking idempotency for that slice. Pagination/stop-condition
  logic is copy-pasted across 3 HTML pipelines (drift risk). Converge the directory upsert on
  `ON CONFLICT DO UPDATE` (already used in `enrich_websites`).
- **multiagent_db:** `db.py` sets `busy_timeout` + WAL but not `synchronous=NORMAL` (the standard
  WAL companion); no checkpoint management while long resolution reads can starve WAL truncation.
  Lock-retry loops catch any `OperationalError`, not specifically "database is locked" (a mid-flight
  migration would be retried instead of failing fast). Convert the COORDINATION status-board /
  decisions regions to strictly append-only to remove the only merge-conflict surface.
- **tooling:** `run.py`'s score→band mapping (auto/pending/rejected/dropped) is the one untested
  critical path. No end-to-end resolution test on realistic multi-source data (only `sample_eval`,
  which prints). `pytest-cov` declared but never wired. `scripts/` mixes a useful recon archive with
  spent one-off DB-mutating scripts.
- **docs_direction:** **README is frozen at Milestone 1** ("Pre-alpha," "Pilot scope is Arizona")
  and twice references a non-existent `docs/decisions.md` — the front door sends readers to a dead
  file instead of the excellent `assumptions.md`. `click` is a declared-but-unused dependency.

## Cheap wins (low effort) — proposed owners

| # | Win | Proposed owner |
|---|-----|----------------|
| C1 | Rewrite README to current state (national, ~406k, resolution in progress); fix the two dead `docs/decisions.md` links → `assumptions.md`; point to AGENTS/DEVELOPER/COORDINATION | Cleanser |
| C2 | Add `.github/workflows/ci.yml` (setup-uv → `make check` + `make test`) | Cleanser (Mastermind sign-off — affects all merges) |
| C3 | Move spent one-off DB-mutating scripts to `scripts/oneoff/` (keep recon archive) | Cleanser |
| C4 | `db.py`: add `PRAGMA synchronous=NORMAL` + periodic `wal_checkpoint(TRUNCATE)` | Mastermind (shared infra; Cleanser can draft the patch) |
| C5 | Drop unused `click` from `pyproject.toml` deps (or adopt it project-wide) | Mastermind (owns deps) |
| C6 | Converge the directory upsert on `ON CONFLICT DO UPDATE` | Mastermind / pipelines owner |
| C7 | `fuse_attorney_count` → robust aggregation (not `max()`) | **Canonizer (OPEN ITEM 1 — not Cleanser)** |
| C8 | Fix `looks_like_firm` at source + drop the `is_firm_name` fork | Canonizer / Mastermind (affects parsers + enrichment) |

## Recommended sequencing

The team is heads-down on resolution *correctness* (floors/caps, fusion robustness, multi-domain) —
good and necessary. But the three things that decide whether the project *succeeds* are currently
unowned or unmeasured: **measurement (P1), the business-output layer (P3), and the Postgres cutover
(P5).**

> **backfill (P4, queued) → eval harness (P1) → Splink pilot decision (P2) → canonical widening +
> deal-target scoring (P3), with the Postgres cutover (P5) landing at the website-as-source point.**

Postgres (infra / Mastermind) and Splink (quality / Canonizer) are **largely independent tracks** and
can proceed in parallel. Splink's DuckDB backend means it is **not blocked on Postgres**. If the
recompressed Canonizer must pick one next imperative, it is the **eval-harness → Splink** track
(Postgres is Mastermind-led). Detailed Postgres + Splink plans are forthcoming in `docs/audit/`.
