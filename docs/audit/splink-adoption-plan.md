# Plan — adopt Splink for matching & clustering

**Author:** Cleanser · **Date:** 2026-06-08 · **Owner of execution:** Canonizer (resolution
domain), recompressed with a Splink imperative · **Status:** proposal for Mastermind sign-off +
Canonizer pickup.
**Trigger (audit P1/P2):** the match layer is a hand-tuned weighted-additive score with a *growing*
stack of hard floors/caps (`WEBSITE_MERGE_FLOOR=88`, `PHONE_NAME_FLOOR=86`, `NAME_LOCATION_FLOOR=86`,
`NAME_CONFLICT_CAP=55`, `WEBSITE_CONFLICT_CAP=50`) + bespoke `_UnionFind`, validated only by ~20-firm
eyeball dry-runs. That is a hand-approximation of Fellegi-Sunter, and the constants won't be known to
generalize at ~406k records (+49 state bars pending).

## What Splink is (idiomatic, lead-with)
[Splink](https://moj-analytical-services.github.io/splink/) (MIT, moj-analytical-services) is the
standard Python probabilistic record-linkage engine: Fellegi-Sunter with **unsupervised EM** that
*learns* per-field `m`/`u` match weights from the data (no training labels needed), native blocking,
and built-in connected-components clustering. It replaces — with one maintained library — exactly the
three hand-rolled pieces: `blocking.py`, the `scoring.py` weights+floors+caps, and `_UnionFind`.

## Scope: what Splink replaces vs what STAYS
| Layer | Today | After |
|---|---|---|
| Blocking | `blocking.py` (phone / website / name-prefix+state keys) | Splink `blocking_rules_to_generate_predictions` |
| Pairwise score | `scoring.py` weighted sum + floors/caps | Splink comparisons → **learned** match weights |
| Clustering | `apply.py` `_UnionFind` | `linker.clustering.cluster_pairwise_predictions_at_threshold` |
| **Field fusion** | `fusion.py` truth-discovery survivorship | **UNCHANGED** — runs on Splink's clusters |
| Identity helpers | `is_identity_website` / `is_firm_name` | **KEPT** as pre-processing / fusion gate |

**Key point: Splink does matching+clustering, NOT field fusion.** `fuse_cluster` (reliability/recency
survivorship) still runs per Splink cluster; `rapidfuzz` stays for name-variant grouping in fusion.
So the bespoke work that is genuinely warranted (the data-fusion layer) is preserved.

## Backend & where it runs (the sequencing crux)
**DuckDB** (Splink's default; packaged, no install). Our ~406k records sit well inside DuckDB's
"1–2M on a laptop, ~1 min" comfort zone (Splink benchmarks 7M in ~2 min). Splink reads an **extract**
(a DataFrame / Parquet of the FSR identity columns), so it is **decoupled from the operational
store** — it does NOT need Postgres, and Splink's own Postgres backend is "relatively new / not
perf-tested," so even after the PG migration, **run Splink on DuckDB.** → Splink is **not blocked on
Postgres**; the two tracks parallelize.

## Input shape
One row per `FirmSourceRecord` (the `source="website"` rows included — they're uniform input, which
is the whole point of the MDM decision):
`unique_id = FSR.id`, `name_normalized`, `phone_normalized`, `website_identity` (the bare domain
**only when `is_identity_website`**, else NULL — pre-filter so platform/aggregator domains don't
block), `primary_city`, `primary_state`, `source`. (`primary_*` depend on the P4 backfill — same
prerequisite as today.)

## Proposed settings (maps 1:1 to today's signals)
```python
import splink.comparison_library as cl
from splink import DuckDBAPI, Linker, SettingsCreator, block_on

settings = SettingsCreator(
    link_type="dedupe_only",
    blocking_rules_to_generate_predictions=[      # replaces blocking.py keys
        block_on("phone_normalized"),
        block_on("website_identity"),
        block_on("substr(name_normalized, 1, 8)", "primary_state"),
    ],
    comparisons=[                                  # replaces scoring weights + floors/caps
        cl.JaroWinklerAtThresholds("name_normalized", [0.92, 0.85, 0.70]),
        cl.ExactMatch("phone_normalized").configure(term_frequency_adjustments=True),
        cl.ExactMatch("website_identity").configure(term_frequency_adjustments=True),
        cl.ExactMatch("primary_city"),
        cl.ExactMatch("primary_state"),
    ],
    retain_intermediate_calculation_columns=True,  # for waterfall explainability
)
linker = Linker(df, settings, DuckDBAPI())
```
**Why this subsumes the floors/caps:** the multi-level name comparison + learned weights make
"matching identity website + agreeing name" dominate *automatically* (today's `WEBSITE_MERGE_FLOOR`)
and "two conflicting firm-like names" penalize *automatically* (today's `NAME_CONFLICT_CAP`) — no
hand-set constants. **Term-frequency adjustments on phone** directly address the toll-free lead-gen
guard (a phone shared by 500 records carries far less weight than a rare one) — learned, not hard-coded.

## Training (the unsupervised EM Alex wants led-with)
```python
linker.training.estimate_probability_two_random_records_match(
    [block_on("phone_normalized", "website_identity")], recall=0.7)   # deterministic seed
linker.training.estimate_u_using_random_sampling(max_pairs=1e7)        # u from random non-matches
linker.training.estimate_parameters_using_expectation_maximisation(block_on("phone_normalized"))
linker.training.estimate_parameters_using_expectation_maximisation(block_on("website_identity"))
linker.training.estimate_parameters_using_expectation_maximisation(
    block_on("substr(name_normalized,1,8)", "primary_state"))
```
EM learns the `m`-probabilities (agreement | match) per comparison level — the thing the floors/caps
were hand-approximating. This also retires the fixed `SOURCE_RELIABILITY` priors: source can enter as
a comparison / TF dimension and have its weight *learned*.

## Inference, clustering, threshold
```python
pred = linker.inference.predict(threshold_match_probability=0.9)
clusters = linker.clustering.cluster_pairwise_predictions_at_threshold(pred, 0.95)
```
`cluster_id` replaces `_UnionFind`'s components and feeds `fuse_cluster` unchanged. The **threshold is
chosen on the P1 eval set** (precision/recall on the labeled pairs + the known-firm oracle) — turning
today's eyeballed 85/60/40 into a measured operating point. Splink's match weights + waterfall charts
give per-decision **explainability** the opaque floor constants never did.

## Migration is low-friction (leverages the existing score/policy split)
`match_review_queue` already stores `score_components` + a thresholds snapshot and is **decoupled from
`apply.py`** (apply reads approved pairs → clusters → fuses). So: write Splink's pairwise
`match_probability` into `match_review_queue` (a new component), and `apply.py`'s union-find step is
the only thing swapped for Splink clustering. `fusion.py` and the canonical write are untouched.

## Bonus: resolves two open items more naturally
- **Multi-domain firms (Canonizer OPEN ITEM 2):** Thompson & Hiller / Dickinson Wright cluster via the
  name + phone + city/state comparisons even *without* website agreement — no special-case
  cross-domain pass; the learned weights handle it. A post-cluster size-gate stays available if needed.
- **attorney_count robustness (OPEN ITEM 1):** unaffected — it's *fusion*, not matching — so it
  remains Canonizer's robust-aggregation fix (union floor + corroboration gate). Do it independently.

## Validation gate (do NOT cut over without this)
1. Build the **P1 eval harness first** (labeled pair set stratified by blocking key + score band;
   precision/recall/pairwise-F1; extend `sample_eval`).
2. Run Splink + current floors/caps on the **same** labeled set + the known-firm oracle (Snell &
   Wilmer, Morgan & Morgan, Kutak Rock, the multi-domain cases, the toll-free lead-gen negatives).
3. Adopt Splink **iff** it matches/beats the hand-tuned system on the metric. If it underperforms on
   a corpus quirk, keep the bespoke matcher — but now as a *measured* choice, not an eyeballed one.

## Dependencies / effort
Add `splink>=4` (DuckDB packaged). Effort: high, but bounded — most logic is declarative settings, and
the oracle + eval set (which Canonizer's validated logic already specifies) are the real work.

## Recommended recompressed-Canonizer imperative
> Build the eval harness (P1) → implement Splink-on-DuckDB replacing `blocking.py` + `scoring.py`
> weights + `_UnionFind`, writing `match_probability` into `match_review_queue`; keep `fusion.py`,
> `is_identity_website`, `is_firm_name`. Validate against the eval set + known-firm oracle before
> swapping `apply.py`'s clustering. attorney_count robustness (OPEN ITEM 1) proceeds independently.
> Runs on DuckDB regardless of the Postgres track.
