# Mastermind handoff / self-context

You are **Mastermind** — the coordinator/integrator agent for `legal-deal-sourcing`. This doc is
your context after a compression. Read it, then `git pull origin main` and read `COORDINATION.md`
(the live channel) + `docs/audit/` (Cleanser's plans) to catch anything newer than this snapshot
(~2026-06-08).

## The project (one paragraph)
A Python pipeline that scrapes U.S. law-firm directories (AZ Bar, Martindale, Justia, FindLaw,
generic state-bars) + enriches from firms' own websites → normalizes → resolves the same firm
across sources into a canonical record, for **Bow Street internal deal-sourcing research (NOT
republication)**. Six layers: SCRAPE → PARSE → SOURCE (`firm_source_records`) → RESOLVE (block →
score → cluster) → CANONICAL (`firms` + links, per-field provenance) → ENRICH. Scraping is national.

## Your role + what you own
You are the **coordinator/integrator**, not a feature agent. You own:
- **The coordination channel** (`docs/COORDINATION.md`) — keep it current, route decisions, integrate.
- **Schema + migrations** (alembic) — *only* Mastermind runs migrations; quiesce writers / use online DDL.
- **Shared infra** — `src/legal_sourcing/db.py` (`make_engine`: busy_timeout=30s + WAL; soon
  dialect-aware for Postgres) and the source-record upsert path.
- **Shared-table DB operations** — `backfill_primary_address`, the post-scrape sequence below.
- **The Postgres cutover** (audit P5) — yours to execute.
- **Provisionally P3** (deal-target scoring) — escalated to Alex for an owner; you'd own the plumbing.
- You do **NOT** own the Martindale scrape anymore — see Monitor below.

## The multi-agent setup (FIVE agents now)
| Agent | Branch / worktree | Role |
|---|---|---|
| **Mastermind** (you) | `Mastermind` · `…/legal-deal-sourcing-mastermind` | Coordinator / integration / schema / infra |
| **Websites** | `Websites` · the **main checkout** `…/legal-deal-sourcing` | Firm-website source (re-architecting to `source="website"`) |
| **Canonizer** | `Canonizer` · `…/legal-deal-sourcing-canonizer` | Entity resolution (being recompressed onto Splink) |
| **Cleanser** | `Cleanser` · `…/legal-deal-sourcing-cleanser` | **Read-only** project auditor (writes findings only) |
| **Monitor** | a **Mastermind fork** | Administers the Martindale scrape — **OUTSIDE your purview** |

- **Shared ONE database**: SQLite (WAL) at the **main checkout's** `data/legal_sourcing.sqlite`
  (~417k `firm_source_records`). Every worktree's gitignored `.env` sets `DB_PATH` (+
  `RAW_DATA_DIR`/`PROCESSED_DATA_DIR`) to that **absolute** path. **All DB access via
  `legal_sourcing.db.make_engine()`.**
- **Coordination = git + `main`** (`COORDINATION.md`): pull to read; edit **only your own `###`
  section** with **timestamped** bullets (`YYYY-MM-DD HH:MM UTC`); `git push origin Mastermind:main`
  (rebase on reject). Protocol is in the doc header.

### Monitor (the scrape admin) — do not touch the scrape
Alex forked Mastermind into **Monitor** to administer the Martindale national scrape. The scrape
(national R–Z, capped 25 pages/city @ 0.8 rps, windowless, resumable from a checkpoint under
`data/processed/`) is **Monitor's job now**. Don't babysit it, don't re-tune RPS, don't watch its
log. Your old throttle watcher is gone. If you need the scrape paused/resumed (e.g. for the Postgres
cutover's writer-quiesce, or a migration), **coordinate with Monitor** via the channel — don't kill
its process yourself.

## Current state (snapshot ~2026-06-08)
- **Corpus**: ~417k `firm_source_records` — martindale 81%, justia 10%, az_bar 7%, findlaw 2%.
  `source="website"` = 0 (loader not run yet). `firms`/links/`match_review_queue` = 0 (no canonical
  run yet). `website_enrichment` = 27,099 rows (~20.7k verified). **~60% of records are nameless**
  (justia 100%, martindale 58%); `primary_city`/`primary_state` NULL on 100%.
- **Website enrichment**: the ~27k crawl is COMPLETE. Being **re-architected into a first-class
  source** (`source="website"` rows in `firm_source_records`) — Websites builds a re-extract→FSR
  loader from **cached raw (no re-fetch)**.
- **Canonizer**: hand-rolled truth-discovery resolution built + dry-run-validated (`fusion.py` /
  `identity.py` / `apply.py`, ~289 tests), **HOLDING** for the backfill; being recompressed onto Splink.
- **Cleanser**: delivered a direction audit + Postgres plan + Splink plan + data-quality audit +
  cheap-win proposals, all under `docs/audit/`.

## Big in-flight decisions / plans
1. **website = a source** (DECIDED 2026-06-08): emit websites as `source="website"` FSR rows —
   `FirmSourceRecord` already has every field, so **no migration**, uniform fusion, drops the
   special-case `WebsiteEnrichment` join. `website_enrichment` stays as the crawl cache.
2. **Postgres migration** (`docs/audit/postgres-migration-plan.md`; **YOURS** to execute): SQLite→PG
   at the **website-as-source cutover point** (two writers on FSR trips the logged PG trigger). MVCC
   + JSONB/GIN + per-role GRANTs (DB-enforced lanes). Front-loadable: land dialect-aware
   `make_engine`/upsert + `JSON→JSONB` variant + a Docker-PG CI job **before** the live cutover, so
   the window is just quiesce→`alembic upgrade`→data-move→verify→repoint `.env`→resume. Rollback =
   repoint `DB_URL` back to SQLite (kept read-only-archived). **Awaiting Alex:** managed (RDS/Cloud
   SQL) vs local Docker; driver `psycopg[binary]>=3.2`; data-move (pure-Python chunked copy on
   Windows, or pgloader via WSL).
3. **Splink** (`docs/audit/splink-adoption-plan.md`; **Canonizer's**, via the recompress): replaces
   `blocking.py` + `scoring.py` weights/floors/caps + `_UnionFind` with Splink (Fellegi-Sunter,
   **EM-learned m/u weights**, **DuckDB** backend — NOT blocked on Postgres). `fusion.py` /
   `rapidfuzz` / identity helpers STAY. Writes `match_probability` into `match_review_queue`; only
   `apply.py` clustering swaps. Gated on the eval harness; adopt iff it beats the bespoke matcher.
4. **P3 — deal-target scoring** (the business-goal gap, **UNOWNED**): canonical `firms` is
   scalar-only; child tables empty; **no deal-target scoring/ranking step exists**. Escalated to Alex.
5. **Cheap-wins** (Cleanser drafted as proposals in `docs/audit/cheap-wins/`): Cleanser owns C1
   (README), C2 (CI — you signed off), C3 (`scripts/oneoff/`). **Yours:** C4 (`db.py`
   `synchronous=NORMAL` + `wal_checkpoint`), C5 (drop unused `click`), C6 (converge directory upsert
   on `ON CONFLICT`) — **fold C4/C6 into the `make_engine`/upsert refactor (= the Postgres prep)**,
   don't touch those choke points twice.

## YOUR queued post-scrape sequence (gated on the scrape finishing — Monitor will signal; coordinate)
1. **Fix the martindale office-address parser** (street dumped into `offices[].city_raw`, `state=null`)
   — blocks `primary_state` on **76,579** martindale rows. Do this *before* the re-load.
2. **`martindale enrich`** (firm-profile pass) — recovers names for the ~58% nameless martindale rows.
3. **website-FSR-load** (Websites; can run concurrently — read-from-cache + chunked writes).
4. **`backfill_primary_address`** — already HARDENED (chunked + `make_engine`, concurrent-safe);
   consider a STORED generated column or a pre-`apply` assertion so `primary_state` can't go stale.
5. **Greenlight the Canonizer full run** — only after `primary_state` is populated + the eval harness
   + the Splink-vs-bespoke decision.
Concurrency is safe (WAL + busy_timeout + commit-retry + chunked writes — proven by the overnight
Martindale+website-enrich coexistence). Only the *final/authoritative* backfill + canonical run want
the complete corpus; mid-scrape runs are provisional (resolution is idempotent → re-run is free).

## Decisions AWAITING Alex (the gate — nothing big proceeds until these)
1. Recompress the Canonizer now (eval-harness first)? *(I recommend yes.)*
2. P3 / deal-target-scoring owner? *(I own plumbing; criteria defined with Alex.)*
3. Postgres **managed vs local**? *(Bow St infra/cost call.)*
4. Confirm Postgres cutover at the website-as-source point?
5. Start the **front-loadable, non-destructive** Postgres prep now (carries C4/C5/C6)?

## Canonizer recompress imperative (drafted; apply on Alex's go — also in the Splink plan)
> Your hand-rolled resolution stays the baseline/oracle. Pivot from *expanding* it to *measuring then
> replacing*: (1) **Eval harness FIRST** — labeled pair set stratified by blocking key + score band;
> extend `sample_eval` to report precision/recall/pairwise-F1 (+B-cubed); fold in the known-firm
> oracle (Snell & Wilmer, Morgan & Morgan, multi-domain Thompson & Hiller / Dickinson Wright,
> toll-free lead-gen negatives). (2) **Splink-on-DuckDB** replacing `blocking.py` + `scoring.py`
> weights + `_UnionFind`; write `match_probability` into `match_review_queue`; keep `fusion.py`,
> `is_identity_website`, `is_firm_name`; adopt iff it beats the bespoke matcher on the eval set. (3)
> **Stop expanding** hand-rolled floors/caps. (4) **Robust headcount** (OPEN ITEM 1): union-floor +
> corroboration gate + trimmed mean, not `max()`. Postgres is Mastermind's parallel track; Splink
> runs on DuckDB regardless.

## How to work with Alex (CRITICAL — from the memory + reinforced all session)
- **Research the idiomatic/standard approach and LEAD with it.** (He corrected hand-rolled
  deterministic resolution → truth discovery → now Splink; widening a table → website-as-source.)
- **Check in at boundaries with REAL evidence** (counts, command output, tables) — not summaries.
- **Plan + ask before big/underspecified decisions.** He answers concrete technical questions and
  dismisses vague "what next?" prompts. **Flag risks loudly.**
- **Small commits, clear messages, NO `Co-Authored-By: Claude` trailer.** Push only when asked —
  but he has standing-asked Mastermind to push shared infra to `main`.
- **Use `uv`** (`~/.local/bin/uv run …`; not on PATH). Don't write throwaway scripts — extend
  committed tooling. Python 3.14 runtime (use `safe_urlparse`, not bare `urlparse`).

## Operational gotchas (Windows / PowerShell / git)
- **Run python via `uv run --directory <worktree> …`** so CWD = that worktree (its `.env` resolves
  the absolute `DB_PATH`). Bare `.venv` python with a drifted CWD → relative `db_path` → "unable to
  open database file". (`make_engine` reads `get_settings()` which reads `.env` from CWD.)
- **Git push**: commit on `Mastermind`, then `git pull --rebase origin main && git push origin
  Mastermind:main`. The "NativeCommandError" PowerShell prints on `git push` is **cosmetic** (git's
  stderr) — the push succeeds (check the `..` ref line / `git log origin/main`).
- **Multi-line commit messages**: PowerShell here-strings inside `if`/`;` chains **break** (the body
  word-splits into pathspecs). Use **bash** `git commit -m @'…'@`-free: prefer a bash heredoc, or
  `$msg | git -C $wt commit -F -` (adds a harmless BOM), or a single-line `-m` with no inner quotes/parens.
- **COORDINATION edits**: it changes constantly (4+ agents push). **Pull first**; the Edit tool needs
  a fresh **Read-tool** read after a pull; anchors wrap ~95 chars so watch for line-wrap mismatches;
  edit only the Mastermind/shared regions; pull-rebase-push.
- **Detached/windowless launch** (rarely needed now — scrape is Monitor's): WMI `Invoke-CimMethod
  Win32_Process Create` with `Win32_ProcessStartup ShowWindow=0` (SW_HIDE). `CREATE_NO_WINDOW`
  `CreateFlags` is **rejected** (ReturnValue 21).

## Key files
- `docs/COORDINATION.md` — live channel + protocol. `docs/assumptions.md` — append-only decision log.
- `docs/audit/` — Cleanser: `2026-06-08-direction-audit.md`, `postgres-migration-plan.md`,
  `splink-adoption-plan.md`, `2026-06-08-data-quality.md`, `cheap-wins/`.
- `docs/{canonizer,websites,cleanser}_handoff.md` — the other agents' handoffs.
- `AGENTS.md` / `DEVELOPER.md` — project orientation. `src/legal_sourcing/db.py`, `config.py`,
  `models/`, `resolution/{blocking,scoring,fusion,identity,apply,run}.py`, `pipelines/`,
  `scripts/backfill_primary_address.py`.
