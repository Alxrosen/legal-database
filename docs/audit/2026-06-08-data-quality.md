# Data-quality audit — `firm_source_records` (read-only snapshot)

**Author:** Cleanser · **Date:** 2026-06-08 15:33 UTC · **Method:** read-only `make_engine` queries.
**Caveat:** the Martindale national scrape is live, so counts drift by a few rows between queries
(`417,726` vs `417,728` observed) — treat as a snapshot, not a frozen count.

## Corpus shape
**417,726 source records.** martindale 339,684 (81%), justia 42,301 (10%), az_bar 28,171 (7%),
findlaw 7,570 (2%). `source="website"` = **0** (FSR-load not run — expected). Canonical tables
(`firms` / `firm_source_record_links` / `match_review_queue`) all **0** (apply not run — expected).
`website_enrichment`: 27,099 rows, **20,680 verified**, 12,798 with `attorney_count_min`.

## Finding 1 (HIGH) — ~60% of the corpus is NAMELESS
| source | empty `name_raw` | of total | note |
|---|---|---|---|
| justia | 42,301 | **100%** | Justia carries no firm name at all |
| martindale | 195,724 | **57.6%** | attorney-grain rows, firm fields blank |
| az_bar | 13,056 | 46.3% | |
| findlaw | 0 | 0% | firm-level cards always named |

**251,081 of 417,726 records (~60%) have no `name_raw`.** Canonical name coverage therefore hinges
entirely on three things landing: (a) the **martindale firm-profile `enrich`** pass (recovers
martindale names), (b) **website-as-source** naming nameless Justia/martindale rows *by merge* via
the website-identity floor, and (c) nameless rows merging into a named sibling via phone/website/geo.
Until then, a canonical run would emit a large population of **nameless firms**. → **@Mastermind**
(enrich) + **@Websites/@Canonizer** (website-as-source). This reframes those steps from "nice to
have" to "what makes the majority of the corpus nameable."

## Finding 2 (HIGH) — the 81% martindale majority is signal-poor *until* enrich + backfill
Per-source identifier coverage:
| source | has phone | has website | nameless |
|---|---|---|---|
| martindale | **4.4%** (14,987) | **0%** (0) | 57.6% |
| justia | 95.9% | 65.1% | 100% |
| az_bar | 48.7% | 24.0% | 46.3% |
| findlaw | ~100% | 96.3% | 0% |

The 339,684 martindale rows (81% of everything) currently have **no website, ~4% phone, 58%
nameless** — so for most of the corpus the *only* possible merge signal is **name + location**, and
`primary_state` is NULL on **100%** of rows (Finding 4). Net: the bulk of the corpus is **not yet
mergeable by any key**. The queued sequence (martindale `enrich` → office-parser fix → `backfill` →
website FSR-load) is correctly prioritized — these numbers quantify *why* a canonical run must wait
for it. → **@Mastermind** (the ordering is right; just confirming the magnitude).

## Finding 3 (MEDIUM) — office-parser bug blocks `primary_state` on 76,579 martindale rows
All 339,684 martindale rows have an `offices` entry, but **76,579 (22.5%)** have
`offices[0].normalized.state = NULL` (the documented "street dumped into `city_raw`, state=null"
parse). So `backfill_primary_address` alone would set `primary_state` on ~263,105 martindale rows
(77.5%) but **leave 76,579 without a state until the office parser is fixed first.** Mastermind has
already committed to fixing the parser before the post-scrape re-load — this is the row count it
recovers. (justia 192, az_bar 3, findlaw 6 — negligible.) → **@Mastermind.**

## Finding 4 (HIGH, confirms audit P4) — `primary_city`/`primary_state` NULL on 100% of rows
0 of 417,726 rows have either populated. The blocking `name_state` key and the name+city+state
scoring floor are fully dormant. Unblocked by the queued `backfill` (gated, per Finding 3, on the
parser fix for full coverage). → **@Mastermind** (queued).

## Finding 5 (MEDIUM) — lead-gen phone concentration is real and severe
Top shared `phone_normalized`: **`+18336461198` appears on 450 records**; next 70, 48, 47, 44, 43,
37, 36. A blind phone-merge would fuse 450 distinct firms into one catastrophic cluster. This
validates two things: the existing toll-free guard is **load-bearing**, and the Splink plan's
**term-frequency adjustment on phone** (a phone shared by 450 records carries near-zero match weight,
*learned* rather than hard-coded) is the idiomatic fix. → **@Canonizer** (Splink comparison design).

## Finding 6 (positive) — contacts coverage is excellent
**410,156 / 417,726 (98.2%)** rows carry `contacts`. The distinct-attorney **union** that floors
the headcount estimate (OPEN ITEM 1) has strong raw material across the corpus. `deactivation_status`
is essentially unpopulated on FSR (417,660 NULL; 67 retired, 1 inactive) — it arrives with the
website source rows. Named records: ~166,645; distinct normalized names 159,733 → only ~6.9k exact
normalized-name duplicates, so most merging will (correctly) come from phone/website/geo + **fuzzy**
name grouping, not exact-name collisions.

## Net read
The corpus is **not yet resolution-ready**, and the data confirms the gating sequence is right:
**martindale `enrich` (names/phones/websites) → office-parser fix (+76,579 states) → `backfill`
(geo) → website FSR-load (names the nameless by merge) → canonical run.** Running `apply` before
these would emit a majority-nameless, largely-unmerged firm set. No new blockers surfaced; the
findings quantify the impact of work already queued, plus the lead-gen-phone hazard for the matcher.
