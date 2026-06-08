# Canonizer — session context / handoff

You are the **Canonizer**: the Claude session that owns **canonical firm resolution** for
`legal-deal-sourcing` (scrape US law-firm directories → normalize → resolve into canonical firms,
for Bow Street **internal deal-sourcing**, NOT republication). Read this whole file, then
`git pull origin main` and read `COORDINATION.md` for the latest cross-session state.

## Environment
- **Worktree**: `C:\Users\AlexanderRosen\alex_work\legal-deal-sourcing-canonizer`, branch **`Canonizer`**.
  The bash cwd tends to drift to `alex_work`; `cd` into the worktree or use `git -C <path>`.
- **uv** is not on PATH: `~/.local/bin/uv run --directory <canonizer> python ...`. Don't write
  throwaway scripts — extend committed tooling. Runtime is Python 3.14 (use `safe_urlparse`).
- **Shared DB** (WAL SQLite, `C:\Users\AlexanderRosen\alex_work\legal-deal-sourcing\data\legal_sourcing.sqlite`):
  ALL access via `legal_sourcing.db.make_engine()` (busy_timeout=30s + WAL). Your worktree `.env`
  already points `DB_PATH` (+ raw/processed dirs) at that absolute path.
- **Disjoint writes**: you WRITE only `firms` / `firm_source_record_links` / `match_review_queue`;
  READ everything else (Martindale→`firm_source_records`, Websites→`website_enrichment`).
- **Coordination = `COORDINATION.md` over git+`main`**: `git pull origin main` to read; edit ONLY your
  `### Canonizer` section (timestamped bullets, `YYYY-MM-DD HH:MM UTC`), `git add COORDINATION.md &&
  git commit`, then `git push origin Canonizer:main`. Mastermind owns schema/migrations and posts
  go-aheads there.

## What's built (all in `src/legal_sourcing/resolution/`, on `main`)
Pipeline: **blocking → scoring → `match_review_queue` (`run.py`) → union-find clusters + fusion →
`firms` (`apply.py`)**.
- `identity.py` — `is_identity_website()` (excludes aggregator/social + website-builder PLATFORM
  domains; reuses `normalize.url.is_aggregator_domain`) and `is_firm_name()` (corrected variant of
  `normalize.name.looks_like_firm`, which has a `" pa"`→"Parker"/"Patrick" substring bug; fixed for
  resolution only — the shared one is flagged to its owner).
- `scoring.py` — `score_pair`: weighted components (name/phone/website/city/state/suffix) PLUS
  strong-identifier overrides: **website-identity match floors into the auto-merge band**;
  **phone + strong-name floors**; **name + city + state floors** (dormant until backfill); and CAPS
  (different identity websites, or two conflicting firm-like names) that win over floors. A MISSING
  name is neutral (`None`), never a 0 penalty.
- `fusion.py` — `fuse_cluster`: field-by-field reliability/recency-weighted truth-discovery vote.
  name (firm-gated, fuzzy-grouped variants → most-supported surface form); phone/website (voted,
  identity domains only); `attorney_count` (SEE OPEN ITEM 1); `year_founded` (derived from a verified
  website's years-in-operation, only when ≥ 5). Writes per-field provenance into
  `firms.field_provenance` (free-form JSON — no schema change).
- `apply.py` — union-find over approved `match_review_queue` pairs → `fuse_cluster` per component
  (joins `website_enrichment` by domain) → writes `firms` + links. SKIPS unidentified singletons
  (no name/phone/website); `--keep-unidentified` overrides. Idempotent (clears firms/links first).
- `sample_eval.py` — **READ-ONLY dry-run harness (no DB writes)** — THE validation tool. Flags:
  `--website / --phone / --name / --random-firms N / --merge-threshold / --derive-location / --show`.
  `--derive-location` previews post-backfill clustering by deriving primary_city/state from offices
  in-memory. Use `--random-firms N` for one self-contained dry-run round.
- Tests: `tests/test_resolution{,_apply,_fusion}.py` — 289 green; `ruff check src tests` clean.

## Current state
- **HOLDING** — the official canonical run (`apply`) has NOT been run. Waiting on Mastermind to run
  `backfill_primary_address` (it populates `primary_city`/`primary_state`, currently NULL on all
  ~266k rows, so the `name_state` blocking key + name+city+state floor are DORMANT) — Mastermind runs
  it AFTER the Martindale full scrape + martindale `enrich`, fixes the martindale office parser, then
  **posts the go-ahead in `COORDINATION.md`**. A persistent DB monitor (`b2z3w2ubk`) also watches for
  `primary_state` to populate. Do NOT run `apply` until that go-ahead.
- Validated via `sample_eval` dry-runs across ~20 real firms: **zero false merges** (Snell & Wilmer,
  Morgan & Morgan [464 recs, person-cards gated out], Kutak Rock, Frank Azar all → one firm; toll-free
  lead-gen solos correctly stay separate). Every failure is an UNDER-merge (the safe direction).

## OPEN ITEMS (priority order — Alex's directives)
1. **[TOP] Redesign `fuse_attorney_count` — `max()` is a fragile stopgap.** Today it's
   `max(verified website count, distinct-attorney union)`. Alex flagged: `max()` is vulnerable to a
   single INFLATED bad parse — one wrong "1000" would dominate (mirror image of the under-count it
   fixed: hensleylegal.com parsed 1 for a 31-attorney firm). **Research the idiomatic robust
   approach and LEAD with it** (truth discovery / robust statistics): the union is a hard FLOOR; the
   verified website count is authoritative ONLY when consistent with corroborating evidence
   (`scope` national/regional/state/local, `office_count`, cluster size) — REJECT/flag outliers (a
   "1000" on a 1-office, state-scope, union-of-2 cluster is almost certainly a parse error; "1000" on
   national + many offices + a large union is plausible). Consider median/trimmed aggregation once
   multiple count estimates exist (esp. after the website-source revamp). Validate via `sample_eval`.
2. **Fix the multi-domain false-negative.** A firm with 2+ distinct domains splits into separate
   canonical firms — e.g. Thompson & Hiller (`thompsonhillerdefense.com` + `grandstrandlaw.com`,
   identical enrichment + a shared phone) and Dickinson Wright (`dickinson-wright.com` +
   `dickinsonwright.com`). Alex's hint: **multiple domains are almost exclusively LARGER firms** — use
   firm size (headcount / `scope` / `office_count`) as a GATE so cross-domain merges are only
   attempted for large firms (small firms rarely have two domains → keeps false-positives down).
   Same-firm corroboration: shared phone, identical/near-identical enrichment (description, offices,
   year), highly similar name. Design this together with item 3.
3. **Website-as-a-source revamp is coming.** Alex is coordinating with Mastermind + Websites to
   revamp `website_enrichment` into a SEPARATE, HIGH-CONFIDENCE source you merge against (like a 5th
   source alongside az_bar/justia/findlaw/martindale), with MORE fields backfilled (a firm NAME —
   fixes nameless Justia-only firms; possibly a canonical-identity hint — helps multi-domain).
   **Watch `COORDINATION.md` for the design + following steps; coordinate with Mastermind; flag
   important considerations to BOTH Alex and Mastermind** before building against it.
4. **Nameless Justia-only firms** — Justia carries no firm name and `website_enrichment` has no name
   field today, so firms seen only in Justia resolve with `name=''`. The website-source revamp
   (firm-name field) is the fix; flagged to Websites in `COORDINATION.md`.

## How to work with Alex (do not skip)
- **Research the idiomatic/standard approach and LEAD with it.** He has corrected sessions for
  hand-rolling instead of leading with the idiomatic method (truth discovery, robust statistics).
- **Evidence over assumption** — validate on REAL data via `sample_eval`; never generalize from one
  example; show real cluster output, not summaries.
- **Dry-run, don't merge** — `sample_eval` is read-only; do NOT run `apply` (writes `firms`) until
  Mastermind posts the go-ahead.
- **Coordinate via `COORDINATION.md`** and flag important considerations to **both Alex and
  Mastermind**. Plan and check in at boundaries.
- Small commits, clear messages, **NO `Co-Authored-By: Claude` trailer**, `git add <specific files>`
  (never `-A`); push (`Canonizer:main`) when coordinating or asked.

## First steps
1. `git -C <canonizer> pull origin main`; read `COORDINATION.md` — has Mastermind posted the
   backfill/go-ahead? Is `primary_state` populated yet?
2. Re-read `docs/assumptions.md` resolution entries (2026-06-03 truth discovery; 2026-06-04 fusion +
   matching; 2026-06-08 robust-headcount / multi-domain / website-as-source) and the `resolution/`
   modules.
3. Begin OPEN ITEM 1 (robust `attorney_count`): research the idiomatic robust approach, propose to
   Alex, then implement + validate with `sample_eval`.
4. Once the go-ahead lands (primary_state populated): re-validate the known firms on REAL backfilled
   data (drop `--derive-location`), then proceed per Alex.
