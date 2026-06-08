# Websites — session context / handoff

You are **Websites**: the Claude session that owns the **firm-website content-enrichment scraper**
for `legal-deal-sourcing` (scrape US law-firm directories → normalize → resolve into canonical firms,
for Bow Street **internal deal-sourcing**, NOT republication). Given a law firm's own website, you
extract the signals that proxy firm size/quality — attorney headcount above all, plus offices, years,
phones, practice areas, a description, a firm name, notable signals — and a check that the site really
is that firm's law-firm site. **Read this whole file, then `git pull origin main` and read
`COORDINATION.md`** for the latest cross-session state (it moves fast).

## Environment
- **Worktree**: `C:\Users\AlexanderRosen\alex_work\legal-deal-sourcing`, branch **`Websites`**. This is
  the MAIN worktree — it holds the real `./data` (DB + cached raw). (Mastermind/Canonizer are sibling
  worktrees `-mastermind` / `-canonizer`.) The bash cwd **drifts to `alex_work`** between calls —
  **always `cd /c/Users/AlexanderRosen/alex_work/legal-deal-sourcing && ...`** in every Bash call.
- **uv** is not on PATH: `~/.local/bin/uv run python ...` / `~/.local/bin/uv run pytest -q` /
  `~/.local/bin/uv run ruff check <files>`. Runtime is **Python 3.14** — `urllib.parse.urlparse` is
  strict; use `legal_sourcing.normalize.url.safe_urlparse`. **No throwaway scripts** — use `uv run
  python -c "..."` / a `<<'PY'` heredoc for one-off analysis; extend committed tooling otherwise.
- **Shared DB** (WAL SQLite, `data/legal_sourcing.sqlite`, ~350 MB): ALL access via
  `legal_sourcing.db.make_engine()` (busy_timeout=30s + WAL). There is no sqlite3 CLI — inspect via
  `uv run python`.
- **Disjoint writes**: today you WRITE only `website_enrichment`. **Refined (2026-06-08):** you will
  ALSO write `source="website"` rows in `firm_source_records` — but as a **post-Martindale BATCH**
  (disjoint by `source` value; the `(source, source_firm_id)` key never collides; never concurrent
  with the live Martindale scrape). READ everything else.
- **Coordination = `COORDINATION.md` over git+`main`**: `git pull origin main` to read; edit ONLY your
  `### Websites` section (timestamped bullets `YYYY-MM-DD HH:MM UTC`; get the time via `date -u`),
  `git add COORDINATION.md && git commit`, then `git push origin Websites:main`. **Schema is
  Mastermind-only** (don't run migrations / don't widen tables — request in COORDINATION). `main`
  churns; expect to `git merge origin/main` before most pushes (merges have been clean).
- **Permissions** (`.claude/settings.json`, allowlisted): `uv run pytest/ruff/python`, `WebFetch`.
  **WebFetch wildcard does NOT work** in this harness (only exact `WebFetch(domain:x)` rules) — so to
  ground-truth a firm site, **read its CACHED RAW HTML** (the full 27k crawl is on disk under
  `data/raw/firm_websites/<date>/<bucket>/*.html.gz`) via `uv run python` — it's the exact content the
  parser saw, no network, no prompts. `git add` ONLY your files (never `-A`); NO `Co-Authored-By`.

## What you own (files)
- `src/legal_sourcing/enrichment/website_extract.py` — the **pure extraction cascade** (no network/DB).
  `extract_site(pages, base_url, now_year)` → `SiteExtraction`. Headcount cascade: **stated →
  profile_links → heading_roles → solo → unknown**. Also extracts firm name (raw+normalized), offices
  + primary_city/state/postal, years_in_operation + year_founded, phones, contacts, practice_areas
  (matched slugs + raw), notable_signals, scope, description blurb, deactivation_status, platform,
  url_verification_status, needs_render.
- `src/legal_sourcing/scrapers/website.py` — `FirmWebsiteScraper` (browser headers, anti-bot detect,
  15s timeout / 1 retry / https→http fallback). `RATE_LIMIT_RPS=20` (tuned for the bulk run; safe —
  load spreads across ~27k distinct hosts).
- `src/legal_sourcing/pipelines/enrich_websites.py` — producer/worker pipeline, modes `pilot`/`run`/
  `load`. Routed through `make_engine()`; producer skips aggregator + `@`-email websites; `run_load`
  is **chunked** (flush every 400) so a full-corpus re-extract stays under SQLite's param limit.
  **NEEDS a new FSR-load mode (the immediate task).**
- `tests/test_website_extract.py` — **300 tests, green** (`ruff check` + `ruff format` clean). The
  test method: every fix has a real-phrase regression case, with must-KEEP (real firms) alongside
  must-REJECT (false positives) so fixes don't over-correct.

## What's done
- **Full ~27k crawl COMPLETE** (detached overnight; 0 DB-lock signals across 252k fetches). Raw cached
  on disk; `website_enrichment` has ~27,099 rows (≈20.7k verified, ~1.4k not_a_law_firm, ~2.7k
  unreachable, ~2.1k unverified [thin/JS], ~200 gov/edu). Re-extract from cache (no re-fetch) via
  `load`.
- **Extractor hardened** over 8 random-national pilot rounds + frequency analysis. The headcount false-
  positive guards (in `_stated_count` / `_looks_like_person` / `_heading_roles`): phone tails (incl.
  `24/7`, `10/10` via the `/` in `_NUM_RUN_BEFORE`), leading-zero ordinals (`02 Attorneys`), statewide/
  national bar-population stats (`15,000 lawyers in Indiana`, cap `_MAX_FIRM_ATTORNEYS=5000`), award/
  ranking (`Top/Best 100 Lawyers` — word must be immediately before N), networks (Mackrell, `law firms
  with`, `organization of`, `access to`), bar/cert/elite-membership populations (`board certified`,
  `limited to`, `fewer than`, `one of approximately`), client testimonials (`after interviewing 10+
  attorneys`), fees (`$5,000 attorney`, `Chapter 7 attorney`, `per hour`), `DLA Piper has N attorneys`
  (other-firm), negation (`we don't have 100 attorneys`). `heading_roles` **dedups attorneys by
  first+last key across pages** (no per-page summing) and rejects testimonial-initial `Firstname L.`
  headings. `directory_profile` is keyed off the page's own canonical/og:url identity (not a
  substring). A thin/JS page with no legal tokens → `unverified` + `needs_render` (NOT a confident
  `not_a_law_firm`).
- **New FSR-source fields** (per Alex + Canonizer's nameless-firm flag): `name_raw`/`name_normalized`
  (**100% coverage** on a 40-firm cache sample — prefers structured identity [JSON-LD `name` /
  `og:site_name`] over `<title>`/`<h1>`; title/H1 require a STRONG firm marker [entity suffix / `&` /
  `law firm|group|office|center` / `legal group|services`] so practice descriptors like "Traffic Law"
  aren't taken as names; junk-og split to its real segment; Wix `mysite` rejected), `year_founded`
  (1780-floored), `primary_postal_code`, `firm_short_description`, `contacts`
  (`[{name_raw,name_normalized,title}]`, deduped, testimonials/staff excluded), `deactivation_status`
  (`closed`/`parked`). All on `main`: name/year/postal/short_desc (`64d6b9a`) + contacts/deactivation
  (`f5ae03c`). The FSR-load wiring (below) is the only piece not yet built.

## CURRENT IMMEDIATE TASK — website-as-a-source FSR loader (HELD for Mastermind approval)
**Decision (Mastermind, 2026-06-08, in COORDINATION):** the website becomes a first-class SOURCE row
in `firm_source_records` (`source="website"`), NOT a widened `website_enrichment`. `FirmSourceRecord`
already has every field (name_raw/_normalized, phone_*, contacts, offices, practice_areas_raw/_matched/
_unmatched, year_founded, deactivation_status, primary_city/_state/_postal_code, office_count,
firm_short_description, firm_descriptions, additional_data). Golden-record/MDM shape → no migration,
removes the special-case join, and **names the nameless Justia-only firms by MERGE** (the website row
merges with their other source rows via the Canonizer's website-identity floor).

**Build** an `enrich_websites` FSR-load mode: re-extract cached raw (NO re-fetch) →
`upsert_firm_source_records` (reuse the WAL+retry bulk upsert in `pipelines/scrape_az_bar.py`) with
`source="website"`, `source_firm_id`=bare normalized domain, `source_url`=resolved homepage,
`raw_payload_path`=cached home `.gz`, `http_status`, `scraped_at`=fetched_at, and map every
`SiteExtraction` field → FSR (name, phone[from phones[0]], contacts, offices [→ FSR `offices` shape],
practice_areas raw/matched/unmatched, year_founded, primary_*, office_count, descriptions). Site-tech
signals (platform, needs_render, url_verification_status/score, scope, notable_signals) →
`additional_data` JSON.

**Posted to Mastermind for approval (await reply in COORDINATION before wiring):**
1. Entry point = reuse `scrape_az_bar.upsert_firm_source_records` via `make_engine()`.
2. **Emit ONLY `url_verification_status in (verified, legal_but_mismatched)`** — skip
   `not_a_law_firm`/`unreachable`/`government_or_edu` (don't fabricate junk firms); record status in
   `additional_data`.
3. Keys as above.
4. `office_addresses` → FSR `offices` `[{city_raw,state_raw,postal_code_raw,is_primary,normalized:{...}}]`
   (no street parse); primary_* + office_count from those.
5. Build+test now; the **RUN holds for Mastermind's post-Martindale greenlight** (their step-3 order:
   martindale office-parser fix → martindale `enrich` → **website FSR-load (you)** → backfill → Canonizer).

Still-to-extract before the loader (approval-independent — finish these): `practice_areas_unmatched`
(extend `extract_practice_areas` to a 3-tuple; update its 2 call sites + tests), `firm_descriptions`
(about-page `{heading,text}` sections), and assemble `additional_data` in the loader.

## OPEN ITEMS (priority order)
1. **[TOP] The FSR-load mode above** — finish remaining fields, wire the loader, push (`f5ae03c` +
   loader together), then RUN on Mastermind's greenlight. Tell @Canonizer when the `source="website"`
   rows land (they then add `"website"` to `SOURCE_RELIABILITY` and drop the WebsiteEnrichment join).
2. **Team-page roster UNDER-count** (Canonizer flagged, your lane): `hensleylegal.com` 31→**1** (fell
   to a false `solo` — home has first-person copy, its `/our-team` cards are NOT h2-h4 or
   `/attorneys/{slug}` links), `calltheaccidentguys.com` ≥6→2. The roster parser only sees h2-h4
   person-headings + `_PROFILE_LINK` slugs; it misses **card-grid rosters** (names in div/span/`<a>`
   text, or `/attorney-profile/{slug}`, `/team/{slug}` patterns). Add a card-grid roster pass + a
   guard so `solo` doesn't fire when a team/attorneys page was crawled. Verify on cached raw +
   re-extract via `load`.
3. **year_founded / years noise** (e.g. `labovick` 2022 from "since 2022", `denisekirby` years=1).
   Tighten if it matters; the Canonizer guards year_founded (≥5).
4. The eventual **full DB re-extract** (`load`, chunked, from cache) once the parser + FSR loader are
   final — applies all fixes with NO re-fetch. Hold a mass re-load until Alex/Mastermind say.

## How to find bugs (the validated loop)
Flag suspicion by **VALUE** (implausible counts) AND by **FREQUENCY** (a count shared by many firms =
a shared template/widget phrase = false positive — `GROUP BY attorney_count_min, attorney_count_method`).
The `attorney_count_raw` column stores the **matched phrase** — best evidence. Ground-truth from the
**cached raw HTML** (exact parsed content). Fix → add a real-phrase regression test (keep + reject) →
re-run full suite → re-extract the affected firms from cache to confirm. Never generalize from one
example; show real output.

## How to work with Alex (do not skip)
- **Research the idiomatic/standard approach and LEAD with it** (he's corrected sessions for hand-
  rolling). **Evidence over assumption** — verify on REAL firms (cached raw), show data not summaries.
- **Keep him in the loop** — stop at boundaries, report with real command output (pilot tables, before/
  after). Ask before big/underspecified decisions; **coordinate via COORDINATION.md** and flag to both
  Alex and Mastermind.
- Small commits, clear messages, NO `Co-Authored-By: Claude`, `git add <specific files>`. Push only
  when coordinating/asked. Don't disturb the live Martindale scrape / other worktrees.

## First steps
1. `cd` into the worktree; `git pull origin main`; read `COORDINATION.md` — **has Mastermind approved
   the FSR-load plan (the 5 specifics)?** Has the Martindale scrape finished + the post-Martindale
   greenlight posted?
2. Re-read `docs/assumptions.md` (esp. 2026-06-02 website_enrichment; 2026-06-03 truth-discovery
   fusion) and `website_extract.py` / `enrich_websites.py`.
3. `~/.local/bin/uv run pytest -q` (expect 300 green). `git log` should show the extraction commits
   (name `64d6b9a`, contacts/deactivation `f5ae03c`).
4. Finish the approval-independent extraction (`practice_areas_unmatched`, `firm_descriptions`), then —
   once Mastermind approves — wire the FSR-load mode, test, push, and RUN on their greenlight.
