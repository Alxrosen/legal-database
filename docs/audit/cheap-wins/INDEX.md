# Cheap-wins — ready-to-land patches

**Author:** Cleanser · **Date:** 2026-06-08 · **Status:** PROPOSALS only — **nothing here is
installed into shared files.** Awaiting Mastermind's lane go-ahead (COORDINATION 2026-06-08 15:15).
Each item lists where it lands, who owns it, and the risk.

| # | Win | Lands at | Owner | Risk | Artifact |
|---|-----|----------|-------|------|----------|
| C1 | Rewrite stale README (Milestone-1 freeze; dead `docs/decisions.md` links) | `README.md` | Cleanser | none (docs) | `README.proposed.md` |
| C2 | Add CI (the 4.5s suite isn't gated on a 4-agent shared `main`) | `.github/workflows/ci.yml` | Cleanser + Mastermind sign-off (gates merges) | low | `ci.yml` |
| C4 | WAL hygiene: `synchronous=NORMAL` (+ checkpoint helper) | `src/legal_sourcing/db.py` | Mastermind (shared infra) | low | diff below |
| C5 | Drop unused `click` dep (all CLIs use argparse) | `pyproject.toml` | Mastermind (deps) | none | diff below |
| C3 | Quarantine spent one-off scripts | `scripts/oneoff/` | Mastermind confirm | **see caveat** | note below |
| C6 | Converge directory upsert on `ON CONFLICT DO UPDATE` | `pipelines/` | Mastermind / pipelines | medium | see Postgres plan |

---

## C1 — README rewrite
Full proposed content in **`README.proposed.md`** (this dir). Apply = replace `README.md` with it.
Fixes: "Pre-alpha / Arizona-only" → current national state; removes the two dead `docs/decisions.md`
links (the real log is `docs/assumptions.md`); points readers to AGENTS/DEVELOPER/assumptions/
schema/COORDINATION/audit. Zero code impact.

## C2 — CI workflow
Full file in **`ci.yml`** (this dir). Apply = copy to `.github/workflows/ci.yml`. Runs
`ruff check` + `ruff format --check` + `pytest` via `astral-sh/setup-uv` on push-to-`main` + PRs.
Pair with branch protection requiring the check. **Mastermind sign-off** since it gates everyone's
merges. (Pin action majors at apply time.)

## C4 — `db.py` WAL hygiene  *(exact diff against current `db.py`)*

```diff
         cursor.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
         # WAL is persisted in the DB header, but assert it so no connection
         # is ever the one to silently leave the file in rollback-journal mode
         # (which would make writers block readers).
         cursor.execute("PRAGMA journal_mode = WAL")
+        # synchronous=NORMAL is the standard companion to WAL: fsync at
+        # checkpoint rather than on every commit. Durable against application
+        # crashes (only an OS/power loss can drop the last commit) and a free
+        # write-throughput gain for the batch FSR-load / enrich writers.
+        cursor.execute("PRAGMA synchronous = NORMAL")
     finally:
         cursor.close()
```

Optional companion (prevents the WAL-growth-under-long-readers footgun the audit flagged) — a new
helper a long-running writer calls periodically (e.g. after each city/batch commit):

```python
def checkpoint_wal(engine: Engine) -> None:
    """Truncate the WAL file. A long-lived reader (resolution materializes all
    rows) pins a snapshot that blocks autocheckpoint; an explicit TRUNCATE after
    it releases reclaims the -wal space so it can't grow unbounded."""
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
```

Both are SQLite-only; they move to the SQLite branch of `make_engine` after the Postgres migration.

## C5 — drop unused `click`  *(diff against `pyproject.toml`)*

```diff
     # CLI
-    "click>=8.1",
```

Verified: zero `import click` in `src/`; all 9 CLIs use `argparse`. (Alternative — adopt `click`
project-wide — is the larger-effort architecture item, not a cheap win.)

## C3 — quarantine spent one-offs  *(CAVEAT — not purely mechanical)*
The audit suggested moving one-off DB-mutating scripts to `scripts/oneoff/`. **Important correction:**
`scripts/backfill_primary_address.py` is **NOT spent** — Mastermind has it queued to run
post-Martindale (`python -m scripts.backfill_primary_address`), so it must stay put. Only genuinely
spent scripts (candidates: `renormalize_addresses.py`, `reparse_martindale_recon.py`) should move,
and moving them **breaks their `python -m scripts.X` module path** (AGENTS.md references some).
→ **Defer to Mastermind** to confirm which are truly spent; on move, update the AGENTS.md run
commands. Low priority.

## C6 — converge the directory upsert
`scrape_az_bar.upsert_firm_source_records` does SELECT-then-INSERT/UPDATE per row; `enrich_websites`
already uses `ON CONFLICT DO UPDATE`. Converging removes one round-trip per record across ~406k rows.
This is **folded into the Postgres migration plan** (the dialect-aware upsert helper) — best done
there rather than twice. Pipelines/Mastermind lane.

## Apply order once the go-ahead lands
C1 + C2 (Cleanser, independent) → C4 + C5 (Mastermind, trivial) → C3 (after Mastermind confirms
spent set) → C6 (with the Postgres work).
