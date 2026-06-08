# Plan — SQLite → PostgreSQL migration

> **STATUS: DEFERRED (2026-06-08) — NOT a current action.** Per Alex (avoid a costly migration
> unless strictly necessary; zero cost) + the reassessment below, Postgres is **not strictly
> necessary now**, a *managed* instance would cost money, and **Splink does not need it** (it runs on
> DuckDB, free). This plan is kept as a **contingency** for when a real trigger actually fires.
>
> **Why deferrable:** SQLite/WAL already serializes *all* writers on one global write-lock regardless
> of table (Mastermind's own 2026-06-08 14:25 note), so website-as-source sharing
> `firm_source_records` doesn't change the concurrency picture; the website rows are **disjoint by
> `source`** (different rows → no lost-update conflict) and the FSR-load is a **one-shot batch
> sequenced after the scrape**, not sustained concurrency. 417k rows / <1 GB is trivial for SQLite,
> and two concurrent writers already ran overnight with 0 lock contention. **Revisit triggers:**
> sustained concurrent writes to the *same rows* from multiple long-running processes;
> multi-host/networked access; or a deal-target analytics layer needing heavy JSONB/GIN beyond
> SQLite. A free *local* Postgres covers those if they ever arrive — **no managed/cloud spend.**

**Author:** Cleanser · **Date:** 2026-06-08 · **Owner of execution:** Mastermind (coordinated,
all-agent cutover) · **Status:** DEFERRED contingency (was: proposal for sign-off).
**Trigger (from the audit, P5):** the `source="website"` decision puts a second writer on
`firm_source_records`, tripping the project's own logged Postgres trigger
(`assumptions.md` 2026-06-04: "two agents need to write the same table → Postgres"). SQLite/WAL is
*many readers + one writer*; it is not a multi-writer store.

## Why Postgres (idiomatic, lead-with)
MVCC + row-level locking (concurrent writers without a global write lock), online `ADD COLUMN`
(DDL against a live writer — the one operation the project flags as unsafe today), **JSONB + GIN**
(this schema is JSON-heavy: `contacts`/`offices`/`practice_areas`/`additional_data`/
`field_provenance`), concurrent index builds, and **per-role GRANTs** that turn the disjoint-write
lanes from honor-system convention into a DB-enforced constraint.

## What makes this low-risk here
The model layer is already Postgres-portable (typed SQLAlchemy 2.0 declarative; the
`uq_firm_primary_contact` partial index already carries `postgresql_where`; `func.now()`
server-defaults are dialect-neutral). The only SQLite-specific code is concentrated in two places:
`db.make_engine()` (the WAL/busy_timeout PRAGMA listener) and the `sqlite_insert(...)` upsert. Both
already route through single choke points.

## Decisions needed from Mastermind first
1. **Where does Postgres run?** Local (Docker) for dev vs a **managed instance** (RDS / Cloud SQL)
   for the shared multi-agent DB. Bow Street context suggests managed; this drives pool/SSL config
   and the cutover window. *(Recommend: managed, with a local Docker PG for tests/CI.)*
2. **Driver:** add `psycopg[binary]>=3.2` (psycopg3, the current idiomatic driver;
   `postgresql+psycopg://`).
3. **Data-move tool:** pgloader (idiomatic) vs a SQLAlchemy chunked-copy script — see "Data move,"
   noting the Windows friction.

## Step 1 — Schema parity (model change; Mastermind's lane)
- **JSON → JSONB.** Make every JSON column emit JSONB on Postgres while staying JSON on SQLite:
  ```python
  from sqlalchemy import JSON
  from sqlalchemy.dialects.postgresql import JSONB
  json_col = mapped_column(JSON().with_variant(JSONB, "postgresql"), ...)
  ```
  (A shared `JSONVariant` type or a tiny `TypeDecorator` avoids repeating it across models.)
- Everything else carries over unchanged. Confirm `String(n)` lengths, CHECK constraints, and the
  partial unique index (already dialect-aware).
- **Alembic:** `env.py` already reads the URL from config — point `DB_URL` at Postgres and
  `alembic upgrade head` builds the schema natively. Keep the single linear head; **no new
  migration is needed for the move itself** (the schema is the same; only the dialect changes).
  The JSON→JSONB variant is metadata-level and emits `JSONB` on a fresh PG build.

## Step 2 — Data move (~406k FSR rows + website_enrichment + reference/taxonomy)
**Idiomatic: pgloader in ORM-schema-first mode** — create the schema on PG via `alembic upgrade
head` first, then pgloader discovers the SQLite catalog and `COPY`s data into the pre-created
tables (casting types; `on error stop` for the authoritative run).
```
LOAD DATABASE FROM sqlite:///.../legal_sourcing.sqlite INTO postgresql:///legal_sourcing
  WITH data only, on error stop, reset sequences
  SET work_mem to '256MB';
```
**Windows caveat (real):** pgloader is Linux/Mac-first; on this Windows host run it via **WSL or a
Docker one-shot**, or use the fallback below.
**Fallback (pure-Python, Windows-native):** a chunked SQLAlchemy copy script — read each table from
the SQLite engine in 5k-row batches, bulk-insert into the PG engine (both via `make_engine`),
JSON dicts insert straight into JSONB. Slower but dependency-light and idempotent per table.
Either way: **the SQLite file is untouched** (it stays the source of truth until cutover is
verified).

## Step 3 — Upsert dialect (`make_engine`-aware helper)
Centralize one dialect-agnostic upsert (also resolves cheap-win **C6**, converging the directory
path off its per-row SELECT):
```python
if engine.dialect.name == "postgresql":
    from sqlalchemy.dialects.postgresql import insert
else:
    from sqlalchemy.dialects.sqlite import insert
stmt = insert(FirmSourceRecord).values(rows)
stmt = stmt.on_conflict_do_update(index_elements=["source", "source_firm_id"], set_={...})
```
Two call sites: `scrape_az_bar.upsert_firm_source_records` (convert SELECT-then-write → ON CONFLICT)
and `enrich_websites._flush` (already ON CONFLICT — just swap the dialect import). The lock-retry
wrapper is **removed on PG** (MVCC handles concurrency); keep it only on the SQLite branch.

## Step 4 — Engine / pool config (`db.make_engine` branches on dialect)
- **SQLite branch:** unchanged (WAL + busy_timeout + the new `synchronous=NORMAL`, cheap-win C4).
- **Postgres branch:** drop the PRAGMA listener; use a real pool —
  `create_engine(url, pool_size=5, max_overflow=10, pool_pre_ping=True, pool_recycle=1800)`
  (tune `pool_size` to the agent count). No `busy_timeout` needed.
- **Lane enforcement (the payoff):** create roles + GRANTs so the lane contract is DB-enforced —
  e.g. `martindale_writer`/`website_writer` (INSERT/UPDATE on `firm_source_records`),
  `canonizer` (write `firms`/`links`/`match_review_queue`), `cleanser_ro` (SELECT only). Each
  worktree's `.env` uses its role's URL. This replaces the honor-system convention the audit flagged.

## Step 5 — Cutover choreography (one coordinated window, Mastermind-led)
1. **Quiesce writers:** pause the Martindale scrape at a checkpoint; ensure no website FSR-load is
   mid-run; Canonizer is holding anyway. (The scrape is checkpoint-resumable → safe to pause.)
2. `alembic upgrade head` against the fresh PG instance.
3. Data move (Step 2).
4. **Verify (gate):** per-table row-count parity SQLite↔PG; spot-check a few JSONB blobs;
   `make test` against the PG URL; a `sample_eval` dry-run reads cleanly.
5. **Repoint** every worktree's `.env` `DB_URL` → PG (role-scoped).
6. **Resume** writers (scrape `full` resumes from its checkpoint).

## Rollback
The migration is *additive* — a new PG instance alongside the intact SQLite file. Rollback at any
point before/after the flip = repoint `DB_URL` back to the SQLite path. Because resolution is
idempotent (`apply` clears + rebuilds), re-running on PG after a rollback-and-retry is free. Keep
the SQLite file read-only-archived for one full cycle after a clean cutover.

## Post-migration wins (do after, not during)
- **GIN indexes** on the JSONB columns that get queried (`field_provenance` for the P3 audit
  questions; `additional_data`).
- **`primary_state` as a `GENERATED ALWAYS AS (...) STORED`** column off the `offices` JSONB
  (resolves P4's "indexed-but-always-NULL" permanently) — only if the primary-office rule is a
  pure row-local projection; otherwise keep the explicit backfill.

## Sequencing vs Splink
**Independent track.** Splink runs on its own DuckDB engine and reads an extract — it does **not**
require Postgres (and Splink's own PG backend is "new / not perf-tested," so even post-migration,
run Splink on DuckDB). Postgres is infra (Mastermind); Splink is quality (Canonizer). The natural
Postgres landing point is the **website-as-source cutover**, since that is the change that breaks
the single-writer-per-table invariant.

## Effort
High (coordinated cutover), but front-loadable: schema-variant + driver + dialect-aware
`make_engine`/upsert can land and be CI-tested against a Docker PG **before** the live cutover, so
the actual window is just quiesce → move → verify → flip.

_Sources: [Splink backends](https://moj-analytical-services.github.io/splink/topic_guides/splink_fundamentals/backends/backends.html) ·
[SQLAlchemy PostgreSQL dialect / JSONB](https://docs.sqlalchemy.org/en/21/dialects/postgresql.html) ·
[pgloader docs](https://pgloader.readthedocs.io/) ·
[Converting to PostgreSQL (pg wiki)](https://wiki.postgresql.org/wiki/Converting_from_other_Databases_to_PostgreSQL)_
