# Canonizer — session context / handoff (2026-06-09, post-Splink-pivot)

You are the **Canonizer**: the Claude session that owns **canonical firm resolution** for
`legal-deal-sourcing` (scrape US law-firm directories → normalize → resolve into canonical firms,
for Bow Street **internal deal-sourcing**, NOT republication). Read this whole file, then
`git pull origin main` and read `COORDINATION.md` for the latest cross-session state.

## Environment
- **Worktree**: `C:\Users\AlexanderRosen\alex_work\legal-deal-sourcing-canonizer`, branch **`Canonizer`**.
  The bash cwd tends to drift to `alex_work`; `cd` into the worktree or use `git -C <path>`.
  (NB `alex_work`'s parent is itself a junk git repo — `AmrosenBS/test` rooted at the HOME dir.
  Never commit there; the project remote is `Bow-Street/law-firm-sourcing`.)
- **uv** is not on PATH: `~/.local/bin/uv run --directory <canonizer> python -m …`. Python 3.14.
- **Shared DB** (WAL SQLite, `…\alex_work\legal-deal-sourcing\data\legal_sourcing.sqlite`): ALL access
  via `legal_sourcing.db.make_engine()`. Worktree `.env` already points `DB_PATH` at it.
  ~450k `firm_source_records`: martindale 352k / justia 42k / az_bar 28k / **website 21k (loaded)** /
  findlaw 8k. Scrape COMPLETE; `primary_state` backfilled (Fixer).
- **Disjoint writes**: you WRITE only `firms` / `firm_source_record_links` / `match_review_queue`.
- **Coordination = `COORDINATION.md` over git+`main`**: edit ONLY your `### Canonizer` section
  (timestamped `YYYY-MM-DD HH:MM UTC` bullets), commit, `git push origin Canonizer:main`.
- Tests: `uv run pytest` (352 green) · lint: `uv run ruff check src tests` + `ruff format`.

## THE BIG PICTURE — where the pivot stands
Alex greenlit replacing the hand-rolled matcher (blocking.py + scoring.py floors/caps +
apply.py `_UnionFind`) with **Splink 4 on DuckDB** (`docs/audit/splink-adoption-plan.md`).
**Status: Splink is built, tuned over 4 rounds with Alex, and BEATS the bespoke matcher on the
human reference.** What remains (in order):
1. **Full-corpus (450k) validation** — everything so far ran on the ~62k-record eval working set;
   confirm λ/threshold/settings hold at production scale (runtime + blocking volume too).
2. **Wire into the pipeline** — write Splink `match_probability` into
   `match_review_queue.score_components`, swap ONLY `apply.py`'s `_UnionFind` for
   `linker.clustering.cluster_pairwise_predictions_at_threshold`. **KEEP `fusion.py` +
   `identity.py` + rapidfuzz** (Splink does match+cluster, NOT field fusion).
3. **OPEN ITEM 1 (robust `fuse_attorney_count`)** — independent of Splink (fusion, not matching).
   Design direction already validated: floor = distinct-attorney union; reject "stated" counts
   grossly discordant with OBSERVED evidence (union + cluster footprint); office_count/scope are
   additive-only corroborators (they're NULL-or-1 on ~79% of sites — never the basis for rejection).

## The two modules you own (both on `main`)
- **`resolution/eval_harness.py`** — the measurement gate. Identity-website + known-firm-oracle
  ground truth → 328k auto-labeled pairs + **40 human labels** (`data/eval/clerical_labels.csv`,
  committed = the project's ground-truth gold). Metrics: pairwise P/R/F1 sweep, B-cubed,
  blocking-recall ceiling. Auto-labels are DE-BIASED (shared phone + name≥92 ⇒ one firm even across
  domains — `_multidomain_same_firm`). CLI: `bespoke`, `clerical-sample`.
- **`resolution/splink_linker.py`** — the engine. CLI: `compare` (head-to-head vs bespoke),
  `tune --variants …`, `prior --lambdas …`, `errors` (per-clerical-pair verdicts + FP/FN dump),
  `sample` (active-learning export), `threshold` (full P/R/F1 curve).

## Locked-in model decisions (each was MEASURED — don't relitigate without new evidence)
- **`DEFAULT_VARIANT = "tuned2_no_pa"`**: TF-name JaroWinkler + name-DERIVATION containment level
  (len≥12, catches "zurich north america" ⊂ "…corporate law division") + near-phone (Levenshtein≤1,
  catches Savela's …101/…001 typo) + website exact (**NO term-frequency** — TF shattered big firms)
  + city + **state-PROXIMITY** (same Census division = partial credit; Silverman NJ/NY).
- **Practice areas: measured and DROPPED** (split Morgan & Morgan, cut precision; 13% coverage,
  0% martindale).
- **`DEFAULT_PROB_TWO_RANDOM = 5e-3`** (prior λ): THE dominant lever. Splink's own estimate (~1e-5)
  makes a website-only match score ~0.06 and shatters multi-office firms. Calibrated to the
  blocked-candidate match rate. `_U_SEED` fixed for reproducibility (without it F1 wobbles ±0.03).
- **Operating threshold is DATA-DERIVED, precision-favoring** (`derive_operating_threshold`):
  highest recall at precision ≥0.98 via Splink's `accuracy_analysis_from_labels_table` → ~0.65.
  The match-prob distribution is **bimodal** (true negatives <0.06, matches ≥0.5), so a "low"
  threshold ≠ uncertainty. Curve is flat 0.53–0.88; the binding constraint is **stay ≤0.88** or
  domain-only merges (eapdlaw intra-floor ~0.89) fall off. Alex explicitly wants the threshold
  derived, never hand-set.
- **SOURCE-AWARE comparison: tried and REVERTED.** Alex's source-semantics principle (az_bar/justia
  = INDIVIDUAL/member-level; website + martindale-/organization/ = FIRM-level) is real, but encoding
  it as a member-involved neutral level on website+phone REGRESSED hard (clerical 29→22, oracle
  shattered, threshold forced to 0.95) — member records are the corpus majority, so neutralizing
  their mismatches removes most discriminating signal. Fellegi-Sunter learns marginal weights, not
  source×field interactions, and EM training is unsupervised — labels don't train weights, only
  threshold+validation. **Accepted misses** (rare tail): Stokes (8226/447577), Merchant & Gould
  (3348/442439, p=0.60 just under 0.65), AZ Supreme Court (3667/10222, bad swlaw.com website).
- **Name-only cross-state is the model's resolution limit**: WCTL 52585/60048 ("merge") and Hunt
  434757/437581 ("don't") are the IDENTICAL data signal — only human brand-knowledge separates
  them; whole-string TF even inverts them. Precision-favoring = accept the WCTL-type miss.

## Final scorecard (62k eval set, seeded, reproducible)
| engine | pairwise F1 | B-cubed F1 | human labels | multi-domain firms |
|---|---|---|---|---|
| bespoke (thr=85) | 0.999* | 0.997* | ~half right | SPLIT (T&H, Dickinson Wright) |
| **Splink tuned2_no_pa @ derived ~0.65** | 0.972 | 0.975–0.980 | **~29/36 era; strong on 40** | **MERGED** |

*Bespoke's near-1.0 on auto-labels is CIRCULAR (its website floor ≈ the labeling rule) — the fair
reference is the human labels + oracle, where Splink clearly wins. Splink's "false positives" vs
auto-labels were verified to be overwhelmingly CORRECT multi-domain merges (3.7k+, e.g.
franktwaterslaw.com↔fortmohavelaw.com) that bespoke's WEBSITE_CONFLICT_CAP refuses.

## Alex's resolution POLICY (captured as labels; binding)
- Attorney/member record merges INTO their firm (Bruno; ClaimsHero; Merchant & Gould).
- Multi-office branches → ONE canonical firm, offices aggregated (Nugent, WCTL). NB Phase-3
  widen-`Firm` must add offices aggregation to `fuse_cluster`/`apply.py` (today `Firm` has no offices).
- Justia no-name + website-only records merge along the DOMAIN (eapdlaw=58-attorney EAPD,
  silvermanthompson, Kutak). Verified working via `website_identity`.
- Gov office + named sub-division = one entity; universities = merge (low-stakes, not the target).
- Generic/containment names ("Legal Services") = HARD REJECT — distinctiveness gates containment.
- Two FIRM-level (website↔website) records with valid distinct domains = DIFFERENT (Payne, Wright).
- Shared office building ≠ same firm (the 106 E. Boone St. solos); "&"-named firm ≠ solo lawyers.
- Similar-sounding unique names (Wieben/Widger) = different; likening factors don't trump.
- Prescott Law Group (4401/9442): Alex UNSURE — left unlabeled, skip.
- Re-scrapes: overwrite-by-key `(source, source_firm_id)` is the robust freshness path; fusion's
  reliability×recency (180d half-life) is only a gentle tiebreaker. Splink clusters; fusion canonizes.

## How to work with Alex (do not skip)
- **Research the idiomatic approach and LEAD with it**; but verify idiomatic ≠ blind (max-F1 was
  idiomatic and wrong; the precision≥0.98 rule was the fix he wanted reasoned out).
- **Evidence over assumption — he checks**: he caught the office-count assumption, the threshold
  smell, and supplies counter-examples from his own DB searches. Always pull the real records
  (`uv run … python -c` + SQL) before arguing.
- **Case-study loop**: he reviews pairings via small AskUserQuestion batches (use record details +
  current Splink prob). Record EVERY verdict in `data/eval/clerical_labels.csv` (committed) with his
  reasoning in the note. He sometimes dismisses a batch — don't block on labels; keep iterating.
- When he asks "why" (e.g. the 0.535 threshold), answer with the actual curve/data, not theory.
- Small commits, clear messages, **NO Co-Authored-By trailer**, `git add <specific files>` (never
  `-A`); push `Canonizer:main` when coordinating.

## Data-quality flags ROUTED (other agents' lanes; don't fix yourself)
- @Websites: 35 `attorney_count=100` extractor false-positives + 1 mis-crawl (id 432144);
  GENERIC extracted names ("LAW OFFICE OF", "Phoenix Law Firm", "Need to update", "Poring168",
  "Estate Planning Attorney") — name extraction needs a generic-stub guard (null > junk).
- @Websites: coverage gap — firms whose directory rows lack a website URL got no website row
  (Kenneth S. Nugent). FindLaw name mis-extractions ("Scott Cohen" pair = two different people).
- Lead-gen exemplar for negatives: `+17623800028` (15 distinct firm domains). NB `+18336461198`/450
  records is Morgan & Morgan's OWN number — a correct single-firm merge, not a hazard.

## First steps for a fresh session
1. `git pull origin main`; read `COORDINATION.md` (any replies from Mastermind/Websites?).
2. Re-run the gate to confirm reproducibility: `uv run … python -m
   legal_sourcing.resolution.splink_linker compare` (expect all oracle firms → 1 cluster, derived
   threshold ~0.65, clerical accuracy consistent with the last COORDINATION entry).
3. Proceed to the NEXT STEP (full-corpus validation → apply.py wiring), or more case studies if
   Alex asks. OPEN ITEM 1 (robust headcount) remains open and independent.
