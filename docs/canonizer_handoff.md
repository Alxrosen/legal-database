# Canonizer — session context / handoff (2026-06-15, canonical DB CREATED)

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
- Tests: `uv run pytest` (428 green) · lint: `uv run ruff check src tests` + `ruff format`.

## THE BIG PICTURE — the canonical DB now EXISTS
Splink 4 on DuckDB replaced the hand-rolled matcher (`docs/audit/splink-adoption-plan.md`). It was
built, tuned over many rounds with Alex, validated on a 50-pair human label set + 32-agent cluster
verification, given a distinctiveness-aware backstop, and **WIRED INTO `apply.py` AND RUN — the
canonical `firms` table is created.**
- **CREATE the canonical DB:** `uv run … python -m legal_sourcing.resolution.apply --splink --threshold 0.5`
  (idempotent: clears+rebuilds `firms`/`firm_source_record_links`; source rows untouched; ~minutes).
  Last run (2026-06-15): **192,599 firms / 242,046 links** from 450,653 records. Morgan & Morgan→1
  firm (468 recs), Kutak Rock (114), Snell & Wilmer (69) correct; the old 437-rec gov hairball is
  gone (split into single entities). ~22k nameless firms (justia-only, website but no name — known).
- **Pipeline:** `apply --splink` → `dry_run.compute_backstop_clusters(session, threshold)` (extract →
  `train_linker` → predict → `backstop_kept_edges` edge filter → `_UnionFind` components) →
  `apply.apply_clusters(components)` → `fuse_cluster` per component (survivorship, UNCHANGED) writes
  `firms`. `fusion.py` + `identity.py` + rapidfuzz are untouched (Splink does match+cluster only).
- **What remains / OPEN:**
  1. **Residual over-merges** (verified, small): 2-firm clusters glued by a phone/website shared
     across only 2–4 firms — **Jacoby & Meyers ↔ J&Y Law** (toll-free, 67-rec cluster), **Holland &
     Hart ↔ Stoel Rives** (shared office phone, 77). Backstop's generic-cutoff is k=5 (needs >5
     distinct firms), so 2-firm lead-gen slips through. Fix options Alex is weighing: lower the
     phone-k, or a conflicting-identity-website rule (which would tension the multi-domain feature).
     Also gov entities sharing a distinctive geo token (e.g. "Pima County Attorney's Office" 93) — may
     be acceptable (one real office) — review.
  2. **ClaimsHero-type recall miss:** Splink scores some correct cross-domain merges just below the
     threshold (website-conflict penalty); the backstop can't add edges Splink didn't propose.
  3. **OPEN ITEM 1 (robust `fuse_attorney_count`)** — independent (fusion, not matching). Floor =
     distinct-attorney union; reject "stated" counts grossly discordant with OBSERVED evidence
     (union + footprint); office_count/scope additive-only (NULL-or-1 on ~79% of sites).

## The modules you own (all on `main`)
- **`resolution/splink_linker.py`** — the engine (extract_frame, train_linker, DEFAULT_VARIANT,
  DEFAULT_PROB_TWO_RANDOM, threshold derivation). CLI: `compare`/`tune`/`prior`/`errors`/`sample`/
  `threshold`. Blocking keys on `firm_name_core` (NOT raw name — the raw "law office" bucket = 300M
  pairs at full corpus; core ≈ 1.1M).
- **`resolution/eval_harness.py`** — the measurement gate. Identity-website + known-firm-oracle
  ground truth → auto-labeled pairs + the human label list (`data/eval/clerical_labels.csv`,
  committed = ground-truth gold). Metrics: pairwise P/R/F1 sweep, B-cubed. Auto-labels DE-BIASED
  (shared phone + name≥92 ⇒ one firm — `_multidomain_same_firm`). CLI: `bespoke`, `clerical-sample`.
- **`resolution/dry_run.py`** — READ-ONLY full-corpus QA + the production clustering function.
  `compute_backstop_clusters(session, threshold)` is what `apply --splink` calls. `backstop_kept_edges`
  is THE backstop (below). The dry-run also prints graph metrics (density/`is_bridge` via igraph),
  threshold-sweep monotonic invariant, generic-value report, a stratified verification sample, and a
  **LABEL REGRESSION** of every `clerical_labels.csv` pair vs Splink-alone AND Splink+backstop.
- **`resolution/apply.py`** — canonical write. `apply --splink` (NEW) = Splink+backstop;
  `apply_clusters(components)` fuses+writes; `apply_decisions` (old queue path) still works.
  `_materialize_firms` is the shared fuse+write loop.

## THE BACKSTOP (Alex's term = a growing known-answer test list + a guardrail that passes it)
Two senses, both live:
1. **The known-answer list** (`data/eval/clerical_labels.csv`, **~50 pairs**, grown by DB sampling):
   should-merge + should-NOT-merge real records. `dry_run` runs it as a regression test every run.
   GROW IT by sampling the DB (esp. should-NOT-merge traps) and re-checking Splink — that's the loop
   Alex wants. Last score: Splink-alone 41/50, **Splink+backstop 42/50**.
2. **The guardrail** = `backstop_kept_edges`: keep a predicted edge ONLY IF the pair shares a
   NON-GENERIC strong identifier (phone/website; generic = on >5 distinct firm name-cores) OR a
   DISTINCTIVE (rare) name token (token in ≤40 distinct firm cores). This is the verified root-cause
   fix: over-merges glue on a common name ("michael"/"smith"/"christopher") or a many-firm
   phone/domain; legitimate low-corroboration merges share their own phone/website or a rare brand
   token ("zurich"/"claimshero"). A BLUNT "require strong id" version scored 37/50 (broke the
   distinctive-name merges) — the distinctiveness-aware version is the keeper.

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
