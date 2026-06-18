# Websites — QA-fix role brief (data-quality loop)

This is your role in the data-quality loop running on `/goal`, **in parallel with Surveyor**. It's
additive to `docs/websites_handoff.md` (your standing context) — read both.

## The one-line job
Surveyor finds extraction bugs and **commits the offending HTML as a fixture + the correct expected
values**. You **iterate `enrichment/website_extract.py` against that SAVED HTML until extraction matches**
— then it's a permanent regression test. You do NOT re-fetch to fix; you fix offline against the fixture.

## Why this works — the data flow you're fixing
Every wrong number Alex flagged lives in `website_enrichment` (the per-domain crawl cache = **your
extractor's output**) and is **fused** into canonical `firms` by `resolution/fusion.py` at `apply` time:
- `firms.attorney_count` ⇐ `fuse_attorney_count(max(verified website count, attorney-union))` ⇐
  `website_enrichment.attorney_count_min` ⇐ `extract_site` headcount cascade.
- `firms.year_founded` ⇐ fused ⇐ `extract_site` year logic.
- `firms.website_normalized` ⇐ `fuse_website` (identity vote) — a mis-attributed site (azbar.org) needs
  the `verify_identity` gate.
So fixing the **extractor** is the leverage point; `firms` is then rebuilt downstream. You cannot edit
`firms` (it's cleared+rebuilt by `apply`).

## The loop (your side)
1. **Pick up** a captured case: a fixture dir `tests/fixtures/qa_cases/<case_id>/{home,attorneys,about}.html`
   + a row in `data/eval/extraction_golden.csv` (`case_id, website, field, expected_value, now_year, note`).
   `tests/test_extraction_golden.py` runs `extract_site` over each fixture and asserts the expected fields —
   a freshly-captured case lands **RED**. That's your work order.
2. **Fix `website_extract.py`** until that test is GREEN — iterating against the saved HTML only (no
   network). Lead with a GENERAL rule, not a per-site hack (the specific extractor changes are your call).
   Loci for the current bugs: the headcount cascade (`_stated_count`, `_heading_roles`, `extract_headcount`),
   `extract_year_founded`, and a new `verify_identity` for website mis-attribution.
3. **Don't break what's green.** Full `pytest` + `make check` must stay green — the growing
   `qa_cases` suite is the no-silent-revert guard. (`data/raw/firm_websites/` is gitignored/empty in
   worktrees, so the regression tests MUST read the committed fixtures, never the cache.)
4. **Re-apply** once a batch of fixes lands: `enrich_websites run` (re-fetch the domains Surveyor flagged
   via `enriched_at`=NULL, plus the **net-new domains** Surveyor discovered for website-less firms) →
   `load-fsr` (refresh `source="website"` FSR rows) → coordinate **`apply --splink --must-link`** with
   @Canonizer (it rebuilds `firms`).

## Identity verification (the azbar.org / walmart.com class)
Build `verify_identity` in `website_extract.py` composing the EXISTING shared predicates
(`normalize/firm_name.py`: `firm_name_core`, `is_low_quality_firm_name`, `domain_consistent`, `host_echoes`;
`resolution/identity.py`: `is_identity_website`, `is_firm_name`): a stored site is only `verified` if its
extracted name matches the firm (or self-echoes its own domain); a bar-assoc/directory/big-company site is
`legal_but_mismatched`. **Dependency:** @Mastermind adds state-bar hosts (azbar.org, americanbar.org, …) to
`AGGREGATOR_DOMAINS` in `normalize/url.py` — coordinate the timing so your gate sees them as non-identity.

## Discovered domains (website-less firms)
When Surveyor finds a strong, location-consistent candidate for a website-less firm, it hands you the
**domain** (not a firm assignment). You crawl it (`enrich_websites`) so it becomes a `source="website"`
record; **@Canonizer merges it on identity** — never assign it to a firm directly (precision-first;
WSChick-AZ vs Schick&Schick-VA must not false-merge).

## Coordination
Parallel with Surveyor, decoupled by the fixture queue + the `to-review` list — keep fixing while it keeps
sampling. Post status in your `### Websites` section (fixtures turned GREEN, parser changes, re-apply runs);
pull before editing; `git push origin Websites:main`.
