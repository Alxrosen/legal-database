# FindLaw — Data Source Reference

Status: **RECON COMPLETE 2026-06-01**. Scraper scaffold + recon script
live. Parser work intentionally deferred pending user practice-area
selection. All structural claims below carry `CONFIRMED 2026-06-01`
where the recon pass verified them; deltas from the original DevTools
notes are flagged inline.

## Why this source

FindLaw is a national US lawyer directory hosted at `lawyers.findlaw.com`.
Different corporate parent (Thomson Reuters) from Martindale (Internet
Brands), so coverage and anti-scraping posture differ. The home page claims
"over 1M firms" in the directory. Useful as a complementary source to
Martindale and Justia, particularly for filling gaps where Martindale's
~167-page-per-city cap bites in mega-cities.

## Critical structural finding from manual investigation

**The directory's actual enumeration is by practice-area × state x city, NOT by
state → city.** This was confirmed by manual investigation:

- The general city page (`lawyers.findlaw.com/alabama/birmingham/`) appears
  to be a curated/landing page with no pagination footer. Only a small
  number of cards are shown.
- The practice-area-by-state-by-city page (e.g.,
  `lawyers.findlaw.com/motor-vehicle-accidents-plaintiff/alabama/birmingham/?keyword=Car+Accident&location=Birmingham%2C+AL&stype=BY_ADDR_OR_ZIP`)
  has proper pagination ("Results 1 to 20 of 36" with numbered page links).
- The "Find a Lawyer" call-to-action button defaults to a practice area
  (Car Accident in observed cases), suggesting practice-area-by-city is the
  intended primary navigation path.

## Implications for scraping

The enumeration becomes:

```
For each practice area (e.g., "motor-vehicle-accidents-plaintiff"):
  For each state (e.g., "alabama"):
    For each city in that state (e.g., "birmingham"):
      For page 1..N where N is the practice-area-city's last page:
        Extract all fl-serp-card.organic on the page
```

**This is much larger than a state-then-city walk.** Rough order of
magnitude:

- ~100 practice areas in FindLaw's index (from `lawyers.findlaw.com/legal-issues/`)
- ~50 states + DC
- Tens of cities per state on average
- 1–3 (though possibly more) pages per practice-area-state-city combination

Raw card fetches across all combinations could half a million. If this seems
to be the case, we'll have to be more selective about which parameters we scrape.
Most of those would be duplicate firms because a single firm
appears under multiple practice areas. Recon should surface the full
practice-area list to the user, as wellas a rough time estimate. The user will then
choose whether to use a subset.

## Site structure — what is known from DevTools (unverified by recon)

### 1. Root state index — `https://lawyers.findlaw.com/`

State list `ul#map-module-state-list` with links to per-state pages. Each
state link is `a.map-module-state-list-link` with href like
`https://lawyers.findlaw.com/alabama/`.

May still be useful as a way to enumerate which state slugs FindLaw uses.

### 2. Practice-area index — `https://lawyers.findlaw.com/legal-issues/`

Has two tabs: "All Legal Issues by Category" and "All Legal Issues by A - Z".
The A-Z tab is at the fragment `#legal-issues-a-z` and renders an
alphabetical list of every practice area.

Each practice area is an `a.fl-list-item-link` with href like
`https://lawyers.findlaw.com/administrative-law/` and text like
"Administrative Law". The trailing slug (`administrative-law`,
`motor-vehicle-accidents-plaintiff`, etc.) is the practice-area identifier
used in URL paths.

Practice-area pages live at `https://lawyers.findlaw.com/<practice-area-slug>/`.

> **CONFIRMED 2026-06-01 with a caveat.** The `a.fl-list-item-link`
> selector matches **124 anchors**, of which the first three are
> navigation links not practice areas: `name-search`, `legal-issues`,
> `profile`. The actual practice-area count is ~121. Parser must
> filter out those nav slugs (or filter to single-segment hrefs
> outside a known set of navigation slugs).
> Full list saved to `data/reference/findlaw_practice_areas.csv` for
> user review. Slugs include the long ones the doc mentioned
> (`motor-vehicle-accidents-plaintiff`,
> `motor-vehicle-accidents-defense`, plus the regular per-area
> entries like `dui-dwi`, `personal-injury`, `bankruptcy`).

### 3. Practice-area state landing — `https://lawyers.findlaw.com/<practice-area-slug>/<state-slug>/`

Believed to list cities within that state for that practice area. Not yet
inspected manually. Recon must document.

> **CONFIRMED 2026-06-01.** The page is a city list, no pagination.
> Probed `/motor-vehicle-accidents-plaintiff/alabama/` → **66
> city links**, every link pointing into
> `/{practice_area}/{state}/{city}/`. No `nav[aria-label="Pagination"]`
> on this page, so we exhaust the city list in a single fetch.

### 4. Practice-area city listings — `https://lawyers.findlaw.com/<practice-area-slug>/<state-slug>/<city-slug>/?...`

The actual paginated listing. Confirmed query parameters from observed
URLs: `keyword`, `location`, `stype`, `page`. Example observed:

```
https://lawyers.findlaw.com/motor-vehicle-accidents-plaintiff/alabama/birmingham/
  ?keyword=Car+Accident
  &location=Birmingham%2C+AL
  &stype=BY_ADDR_OR_ZIP
  &page=2
```

Whether all of these query params are required or whether some are
auto-applied is unknown — recon must determine what the optimal request
parameters are.

> **CONFIRMED 2026-06-01.** Query string params (`keyword`,
> `location`, `stype`) are **NOT required**. Probed
> `/motor-vehicle-accidents-plaintiff/alabama/alabaster/` both bare
> and with the full query string — both returned **40 cards** with
> identical first-card content. The bare URL is the canonical
> entry point; only `?page=N` matters for pagination.

### 5. Per-card structure (from DevTools, unverified)

Each card is `div.fl-serp-card.organic` with `data-testid="organic-card-N"`
for N=1,2,3...

Contents:
- `h2.fl-serp-card-title-wrapper > a.fl-serp-card-title`
  href: detail/profile URL (e.g., `.../alabaster/huntsville/warren--simpson-MjE1MDMxNV8x/`)
  text: attorney or firm name
  attribute: `data-testid="serp-card-title-link"`
- `div.fl-serp-card-text` (`data-testid="serp-card-text"`)
  text: practice-area description, e.g.,
  "Car Accidents Lawyers Serving Alabaster, AL (Huntsville)"
- `div.fl-serp-card-location > span`
  text: street address, e.g., "105 North Side Square, Huntsville, AL 35801"
- `div.fl-serp-card-buttons` contains:
    - `a[data-testid="website-button-link"]` — firm website. Has classes
      including `directory_website`. The href is the actual firm URL with
      `rel="nofollow"`. **Use the `data-testid` attribute as the primary
      selector**; Martindale's button-class shifted (`profile-website-body`
      → `webstats-website-click`), and the same kind of drift is plausible
      here.
    - `a[data-testid="phone-button-link"]` — phone link, href starts with
      `tel:+1...`, contains class `directory_phone`.
- There may be a "Firm Profile" link/button — verify in recon whether
  this is present on all cards, and whether it points to a different URL
  than the title link.

### 6. Pagination — partially confirmed from observed page

The Indianapolis/Birmingham examples show a pagination footer with this
structure (from DevTools inspection):

```html
<nav aria-label="Pagination" class="fl-pagination-nav fl-flex fl-justify-center">
  <ol class="fl-pagination-list fl-no-margin fl-flex fl-justify-center">
    <!-- numbered <li> page links -->
  </ol>
  <a class="fl-button secondary fl-pagination-button"
     aria-label="Next Page"
     rel="next"
     href="...&page=N+1"
     data-testid="fl-pagination-button-next">Next</a>
</nav>
```

Confirmed signals:
- URL pagination parameter: `?page=N` (or `&page=N` when combined with
  other params)
- Next-page anchor: `a.fl-pagination-button[rel="next"]` with
  `data-testid="fl-pagination-button-next"`
- Results-total text: "Results X to Y of N" visible on the page (need to
  find its exact selector during recon — Martindale's was `.results__total`,
  FindLaw's is unknown)
- Page size: 20 cards per page in observed cases (need to verify if
  constant)

Unknown:
- Whether there is a hard page cap like Martindale's 167
- The exact selector for the results-total text (visible to humans but
  selector not yet captured)
- Whether the last-page number is exposed in the DOM in a parseable form
  (Martindale had `input.goToPage[data-max]` — FindLaw may or may not
  have an equivalent)

> **CONFIRMED 2026-06-01** (probed
> `/motor-vehicle-accidents-plaintiff/alabama/birmingham/`):
>
> - Pagination footer `nav[aria-label="Pagination"]` present ✓
> - `a.fl-pagination-button[rel="next"]` present on page 1 ✓
> - Numbered `<li>` page links inside the nav. **`last_page = 2`**
>   for this slice — pagination is short because we're filtered by
>   practice area, NOT all-attorneys.
> - **Page size is 40 cards, not 20.** Page 1 had 40 organic cards;
>   page 2 had 36 (last page). Total = 76.
> - Results-total text "Results 1 to 40 of 36" was found via a
>   forgiving regex `Results?\s+\d[\d,]*\s+to\s+\d[\d,]*\s+of\s+([\d,]+)`.
>   **The "of N" total appears to under-count** (36 vs 76 actually
>   returned across pages). Like Martindale's `results__total`,
>   treat as a soft baseline only.
> - **No hard page cap observed** on this slice. With only 2 pages,
>   we can't confirm. A larger practice-area-city would need
>   probing to detect any cap.

### 7. Sitemaps — listed in robots.txt

- `https://lawyers.findlaw.com/v1/sitemap/sitemap-srps.xml`
- `https://lawyers.findlaw.com/v1/sitemap/sitemap-srps-common.xml`
- `https://lawyers.findlaw.com/v1/sitemap/sitemap-upper.xml`
- `https://lawyers.findlaw.com/v1/sitemap/sitemap-paid-profiles.xml`

Contents and meaning of each are UNKNOWN until recon fetches them. "SRP"
presumably means Search Results Page; these sitemaps may give direct
enumeration of all practice-area-city URLs without walking the index
manually. Worth checking early in recon — sitemap-driven enumeration would
be much cleaner than crawling category and state pages.

> **CONFIRMED 2026-06-01.** All four top-level sitemaps are
> sitemap-index files — each `<loc>` points to a sub-sitemap with
> the actual SRP URLs. Counts:
>
> | Sitemap | Sub-sitemaps |
> |---|---|
> | `sitemap-srps.xml` | **455** |
> | `sitemap-srps-common.xml` | 46 |
> | `sitemap-upper.xml` | 8 |
> | `sitemap-paid-profiles.xml` | 11 |
>
> So full enumeration via sitemaps means fetching ~520 sub-sitemaps
> first, then iterating their `<loc>` entries. That's a usable path
> for a complete sweep — but for the pilot the practice-area state
> landing approach (Phase 4 below) gives a cleaner city list with
> fewer requests.

## Cloudflare protection

Response headers include `Cf-Ray`, `Cf-Cache-Status`, `Cf-Child-Ray` — the
site sits behind Cloudflare. Normal browsing is not being challenged, which
suggests passive Cloudflare configuration rather than aggressive bot
management. However, scrapers can trigger Cloudflare defenses that normal
browsing does not.

Scraper posture for FindLaw should differ from AZ Bar's polite-and-identified
approach:

- **User-Agent: browser-shaped.** Do NOT use the project's identified UA
  (`legal-sourcing-research/0.1 (+contact: ...)`) on FindLaw. Cloudflare
  bot scoring will likely flag identified-bot UAs. Use a realistic recent
  Chrome UA string.
- **Rate: very conservative.** 1 request per 2–3 seconds maximum, single
  thread to start. Ramp up only if recon shows no Cloudflare friction at
  the conservative rate.
- **Treat Cloudflare challenge responses as fatal, not retryable.**
  Signals to detect:
    - HTTP 403, 429, or 503 with a body containing "cloudflare", "Just a
      moment", "Checking your browser", "Attention Required", or similar
    - Server header `Server: cloudflare` combined with non-2xx status
    - HTML response that contains `cf-mitigated` or `__cf_chl_` tokens
  On detection, the scraper should stop immediately, log a clear error
  pointing to docs/data_sources/findlaw_reference.md, and not auto-retry.
  Retrying will burn through retry budgets and may escalate the
  Cloudflare response.
- **Refrein attempt to defeat Cloudflare challenges.** If the program encounters
  significant obstacles like it did with Martindale and cannot seem to continue
  scrapes, tools like Playwright with stealth plugins may be suggested to the user.
  However, none of these should be started without consulting the user first. 

## What we believe is true but should NOT be assumed

- That every card has the same shape. Martindale had at least three card
  variants (full subscriber, basic, "X at Y" in title). FindLaw likely has
  similar variation — recon must surface them.
- That the website button is always present. Many cards on Martindale
  didn't have one; expect similar on FindLaw.
- That title/seniority information is available per card. The screenshots
  shown do not include titles like "Managing Partner" or "Associate." If
  FindLaw doesn't expose titles in the SRP cards, the seniority-replacement
  primary-contact rule cannot be applied to FindLaw data without visiting
  individual profile pages.
- That all four query parameters (`keyword`, `location`, `stype`, `page`)
  are required. Recon should test the minimum URL form that still returns
  full paginated results.
- That practice-area slugs are consistent between the `/legal-issues/`
  alphabetical index and the practice-area-by-city URLs. Verify the slug
  used in (e.g.) `lawyers.findlaw.com/motor-vehicle-accidents-plaintiff/`
  is the same one listed in the legal-issues index.

## Recon requirements

A `scripts/recon_findlaw.py` script must run before scraper or parser work.
The script should run all phases below and update this doc with `CONFIRMED
<date>` annotations on every claim verified, or correction notes on every
claim falsified.

### Phase 1: Robots and sitemaps

- Fetch `https://lawyers.findlaw.com/robots.txt`. Save raw to fixtures.
- Fetch each of the four sitemap URLs listed in robots.txt. Save raw and
  pretty-printed excerpts to fixtures.
- Document what each sitemap contains:
    - Are URLs in `sitemap-srps.xml` practice-area-by-city pages (the kind
      we want to scrape)?
    - Do any sitemaps contain individual attorney/firm profile URLs?
    - How many URLs total in each sitemap?
- Decide: does sitemap enumeration replace some or all of the
  index-walking approach? Update the scraping flow in this doc if so.

### Phase 2: Practice-area index

- Fetch `https://lawyers.findlaw.com/legal-issues/`. Save raw.
- Extract all practice-area links from the A-Z tab via
  `a.fl-list-item-link`. Record the count and the full list of
  slug-to-name mappings.
- Save the full list of practice areas as
  `data/reference/findlaw_practice_areas.csv` for the user to review and
  select a working subset. Format: `slug,name,url`.

### Phase 3: State index (still useful as reference)

- Fetch `https://lawyers.findlaw.com/`. Save raw.
- Extract all state links via
  `ul#map-module-state-list a.map-module-state-list-link`. Expected ~50 US
  states + DC. Record the slug-to-name mapping.

### Phase 4: Practice-area state landing

- Fetch `https://lawyers.findlaw.com/motor-vehicle-accidents-plaintiff/alabama/`.
  Save raw. Document the page structure:
    - Is there a city list? What selector?
    - Is pagination present at the state level?
    - Any other navigation cues that might affect the scraping flow?

### Phase 5: Practice-area city listing (small)

- Fetch `https://lawyers.findlaw.com/motor-vehicle-accidents-plaintiff/alabama/alabaster/`
  with no extra query params. Save raw. Then fetch with the full observed
  query string (keyword, location, stype). Compare responses — is the
  query string actually required?
- For each card, attempt to extract:
    - name (from `a.fl-serp-card-title`)
    - profile URL (href of same)
    - description (`div.fl-serp-card-text`)
    - address (`div.fl-serp-card-location > span`)
    - website URL (`a[data-testid="website-button-link"]` href)
    - phone (`a[data-testid="phone-button-link"]` href, strip `tel:`)
    - any title/seniority text — surface whatever is present, even if it
      doesn't look like a Martindale-style title
- Document card variants observed. Note any card that breaks the expected
  shape.

### Phase 6: Practice-area city listing (large, pagination discovery)

- Fetch
  `https://lawyers.findlaw.com/motor-vehicle-accidents-plaintiff/alabama/birmingham/`
  page 1. Save raw.
- Confirm the pagination structure documented in section 6 above:
    - Selector `nav[aria-label="Pagination"]` exists
    - `a.fl-pagination-button[rel="next"]` exists with
      `data-testid="fl-pagination-button-next"`
    - Numbered page links inside the nav
    - "Results X to Y of N" text — find and document its selector
- Find the last page number from the DOM (text or attribute on the
  numbered links).
- Fetch page 2 with `?page=2`. Confirm URL pattern works and cards differ
  from page 1.
- Fetch the last page. Confirm `rel="next"` disappears or is disabled.
- Document everything in this doc with `CONFIRMED <date>`.

### Phase 7: Cloudflare probe

- During phases 1-6, log response headers and any signs of Cloudflare
  challenges. Record:
    - Whether all requests returned 2xx
    - Any retry events (429, 503)
    - Any HTML containing Cloudflare challenge markers
- If Cloudflare friction appears at the conservative 1-req-per-3-sec rate,
  document the exact symptom and stop. The user will decide whether to
  proceed.

### Phase 8: One attorney profile (depth check)

- Pick one card from the Alabaster recon and fetch its profile URL.
- Document what additional fields are available on the profile that
  aren't on the card (title? practice areas as a list? bar admissions?
  bio?).
- This determines whether the SRP card alone is sufficient or whether
  enrichment via profile-page fetches is required for our use case.

### Output

- Raw responses saved gzipped to `data/raw/findlaw/{date}/recon/...`
- Pretty-printed extractor output saved to `tests/fixtures/findlaw/recon/`
- Practice-area list saved to `data/reference/findlaw_practice_areas.csv`
- Short shape summary printed per phase at the end of the run
- This doc updated with `CONFIRMED 2026-MM-DD` annotations or corrections

## Operational defaults — proposed

These are starting values and are meant to be adjusted.

- `SOURCE_NAME = findlaw`
- `BASE_URL = lawyers.findlaw.com`
- `RATE_LIMIT_RPS = 0.33` (one request every three seconds, more
  conservative than Martindale due to Cloudflare)
- `BURST_CAPACITY = 1` (no bursting on a Cloudflare-fronted site)
- `WORKERS = 1` (single thread during recon and initial pilot)
- `MAX_RETRIES = 3` (lower than other sources — Cloudflare challenges are
  not retryable)
- `USER_AGENT` — **browser-shaped, not the project default.** Use a
  current Chrome on Windows UA. Do NOT include "bot", "research", or a
  contact email in the UA for this source.

## Scraping flow — proposed, pending recon and user practice-area selection

1. **User picks practice areas.** After Phase 2 produces the full list,
   the user selects a subset (probably 5–15 areas) relevant to deal
   sourcing.
2. **For each selected practice area:** fetch the practice-area state
   landing page to get the city list (or use sitemap data if Phase 1
   shows that's cleaner).
3. **For each practice-area × state × city:** fetch list pages 1..N where
   N is derived from pagination metadata.
4. **Per page:** extract every `fl-serp-card.organic` into a raw record
   with the fields documented in section 5 above.
5. **End-of-listing sanity check:** compare scraped card count against
   any declared total. Warn loudly on large mismatches (Martindale-style
   `count_mismatch` warning).
6. **Cloudflare watch:** any challenge response stops the run for the
   user to review.
7. **Optional enrichment:** if Phase 8 shows meaningful added value on
   profile pages (title/seniority data, fuller practice-area info), add
   per-firm profile fetches as a second pass. This decision is deferred
   until after recon.

## Known unknowns to resolve before scraper work

1. Sitemap contents — may simplify or replace the index-walking approach.
2. Whether all query parameters (`keyword`, `location`, `stype`) are
   required, or only `page` matters.
3. The exact selector for the "Results X to Y of N" text.
4. Whether a hard page cap exists (Martindale: 167) and at what value.
5. Whether SRP cards expose attorney titles/seniority.
6. Card shape variants beyond the standard observed in Alabaster.
7. Whether Cloudflare friction appears at conservative rates.
8. The full practice-area list and which subset is relevant to deal
   sourcing (user decision, post-Phase-2).

## Discipline reminders (lessons from prior sources)

- The previous Martindale reference doc had selectors that didn't
  actually work (`:contains()` is not supported by selectolax; the
  firm-website class string in the doc was wrong). **Verify every
  selector against saved HTML before parser work.**
- The earlier AZ Bar work concluded "password rotated" without evidence;
  the actual cause was a missing header. **When recon fails, list ranked
  causes and verify each before concluding.**
- Pagination heuristics that look right can silently terminate after one
  page (Martindale's earlier `_has_next_page` checked for card class,
  which exists on every page). **Whatever pagination logic is built must
  be verified against a city large enough to actually paginate.**
- The previous draft of this doc assumed state → city was the
  enumeration entry point. Manual investigation proved that wrong. **Do
  not finalize architecture based on assumed structure; recon must
  verify the enumeration path itself, not just the selectors within it.**

## Anti-patterns (do not do)

- Do not write the parser before recon completes.
- Do not scrape all practice-area × state × city combinations. The full
  cross-product is several million URLs with massive firm duplication.
  User must select a practice-area subset before bulk scraping.
- Do not use the project's identified User-Agent on FindLaw (Cloudflare).
- Do not attempt to defeat Cloudflare challenges with stealth tooling.
- Do not retry on Cloudflare challenge responses — they are not transient
  and retrying escalates the response.
- Do not assume sitemap structure without fetching it.
- Do not pretend a selector is correct because it "looks right." Confirm
  against saved HTML.
