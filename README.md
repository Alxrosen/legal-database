# legal-deal-sourcing

Alexander M. Rosen — May 21st, 2026

**Purpose:** build a directory of US law firms that can be used for deal sourcing.
Pilot scope is Arizona; sources include state bar directories, Justia, FindLaw,
and firm websites.

## Architecture (sketch)

Data flows through six strictly-separated layers:

1. **Scrape** — fetch raw HTML/JSON, dump immutable gzipped payloads to
   `data/raw/{source}/{YYYY-MM-DD}/`. No parsing, no DB writes.
2. **Parse** — read raw files from disk, produce structured dicts.
3. **Source records** — one row per (source, firm-as-that-source-sees-it),
   preserving raw + normalized fields.
4. **Resolve** — block candidates, score on name/address/phone/website/people;
   auto-merge above threshold, queue mid-confidence for manual review.
5. **Canonical firms** — link table mapping source records to canonical firms
   with per-field provenance.
6. **Enrich** — fill gaps from firm websites, geocoding, practice-area
   vocabularies.

See `docs/decisions.md` and `docs/schema.md` for detail (added in later
milestones).

## Quickstart

```bash
cp .env.example .env
make install
make test
```

## Status

Pre-alpha. Built incrementally against the milestones in the project handoff.

