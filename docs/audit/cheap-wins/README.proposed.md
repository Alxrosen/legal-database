# legal-deal-sourcing

Alexander M. Rosen

**Purpose.** Build a canonical directory of US law firms for **internal deal-sourcing research**
at Bow Street — identifying and evaluating firms as potential acquisition/deal targets.
**Not for republication.** Arizona was the pilot region; scraping has gone national.

## What's here

Sources scraped: **Arizona State Bar, Martindale-Hubbell, FindLaw, Justia**, a generic
**state-bar** scraper (Wyoming wired as the reference state), and **firm websites** (content
enrichment). As of 2026-06 the database holds **hundreds of thousands of source records**
(~406k — one row per firm-as-that-source-sees-it) plus ~27k enriched firm-website crawls; the
Martindale national sweep is still completing.

## Architecture — six strictly-separated layers

Each layer is re-runnable without touching the others:

1. **Scrape** — fetch raw HTML/JSON, dump immutable gzipped payloads to
   `data/raw/{source}/{YYYY-MM-DD}/`. No parsing, no DB writes.
2. **Parse** — pure functions read raw files from disk → structured dicts. No network, clock, or DB.
3. **Source records** — one `FirmSourceRecord` per `(source, firm-as-that-source-sees-it)`, with
   raw + normalized fields side by side.
4. **Resolve** — block candidate pairs, score on name/phone/website/location, auto-merge above
   threshold, queue mid-confidence pairs for review (`match_review_queue`).
5. **Canonical firms** — union-find clusters → field-by-field truth-discovery fusion → `firms` +
   a link table carrying per-field provenance.
6. **Enrich** — fill gaps from firm websites and practice-area vocabularies (geocoding TBD).

## Status

**Active development.** Directory ingestion is largely complete (national); the firm-website
enrichment crawl is complete (~27k sites). Canonical **resolution is built and dry-run-validated**
(zero false merges across the tested firms) but is **not yet run on the full corpus** — it is gated
on the `primary_city`/`primary_state` backfill and a coordination go-ahead. A Splink-based matcher
and a PostgreSQL migration are under evaluation — see `docs/audit/`.

## Quickstart

```bash
cp .env.example .env
make install      # uv sync --extra dev
make test         # uv run pytest
make check        # ruff lint + format check
```

Runs on Python ≥3.11 (developed and run on 3.14). Dependencies are managed with **uv**
(`uv.lock` committed). Pipeline commands and the national-sweep playbook are in `AGENTS.md`.

## Documentation

- **`AGENTS.md`** — orientation, rules of the road, and non-obvious gotchas (read first).
- **`DEVELOPER.md`** — developer setup and workflow.
- **`docs/assumptions.md`** — the **decision log** (dated, append-only: assumption / why / trigger
  to revisit / enforced-where). The authoritative rationale record.
- **`docs/schema.md`** — table-by-table schema reasoning.
- **`docs/data_sources/`** — one file per source (selectors, endpoints, `CONFIRMED` annotations).
- **`COORDINATION.md`** — live cross-session coordination channel for the parallel agents.
- **`docs/audit/`** — project direction audit + the PostgreSQL and Splink plans.

## License

Proprietary.
