# Data source: martindale.com

Status: **planned pilot (M5)** — second source after AZ State Bar.
Last reviewed: 2026-05-29

## 1. What this source is

Martindale-Hubbell is a national attorney directory (Internet Brands; sibling
to FindLaw and Nolo). It covers all 50 states, exposes firm + attorney
records, and unlike the AZ Bar it has reasonably populated "Company" /
firm-name fields. That makes it a much better test for **firm aggregation**
than AZ Bar's alphabetical-start solo skew.

## 2. Why we are not starting with the FindLaw API

The endpoint surfaced in DevTools —

```
GET https://api.findlaw.app/v1/findlaw/wp/v1/api/tagservice/ids/lookup
    ?martindaleOrganizationId=23816&section=martindale.com
```

— is an internal **tag-service lookup**: it maps a Martindale org id to a
set of internal tag ids used by the WordPress front end. It returns 200 OK
but no firm/attorney payload. There is no documented public Martindale
JSON API. Empirically, the data we want lives in the **HTML listing
pages**, embedded as `<script type="application/ld+json">` (JSON-LD)
blocks. That's what we scrape.

## 3. Legal & ethical posture (read before turning the scraper on)

### 3.1 What robots.txt actually says (snapshot 2026-05-29)

```
User-agent: Yahoo! Slurp     Crawl-delay: 10
User-agent: bingbot          Crawl-delay: 3
User-agent: msnbot           Crawl-delay: 3
User-agent: Yandex           Disallow: /
User-Agent: iisbot           Disallow: /
User-agent: GPTBot           Disallow: /*?gam_location
                             Disallow: /

User-agent: *
  Disallow: /legal-news/
  Disallow: /marketyourfirm/mhratings/callClientApi.php*
  Disallow: /document-type/white-papers/articles/
  Disallow: /marketyourfirm/mhratings/callPeerApi.php
  Disallow: /marketyourfirm/wp-login.php
  Disallow: /marketyourfirm/av-300/
  Disallow: /cdn-cgi/
  Disallow: /assets/html/profiles/

Sitemap: https://www.martindale.com/sitemap_profiles.xml
Sitemap: https://www.martindale.com/sitemap_browse.xml
Sitemap: https://www.martindale.com/sitemap_new_profiles.xml
Sitemap: https://www.martindale.com/areas_sitemap_browse.xml
Sitemap: https://www.martindale.com/location_sitemap_browse.xml
```

Key reads for us:

* **We are not blanket-disallowed.** `User-agent: *` blocks only admin / api /
  news URLs. Everything we want — `/find-attorneys/`,
  `/by-location/`, `/areas-of-law/`, individual profiles — is allowed.
* **No crawl-delay is set for `*`.** 
* **Forbidden paths to filter out** at the URL-queue level:
  `/legal-news/`, `/marketyourfirm/*`,
  `/document-type/white-papers/articles/`, `/cdn-cgi/`,
  `/assets/html/profiles/`. A deny-list check before every request
  keeps us honest.
* **Sitemaps are the canonical URL source — look into using them.** See §4.1.

Re-fetch `robots.txt` at the start of every run and write it to
`data/raw/martindale/_robots/<run-ts>.txt`.

### 3.2 Terms of Service

Martindale's ToS prohibits "automated means" of collecting profile data
for commercial republication. Our use is research / internal deal
sourcing, not republication.

### 3.3 No bypassing access controls

Martindale sits behind Cloudflare. If we get a 403 / 503 / interstitial,
**stop and back off**. For the first run, hold off on swapping in TLS-impersonation tooling
(curl-impersonate, got-scraping with Chrome fingerprinting, etc.). We can return to this at a later point.

### 3.4 PII

Profiles include name, firm, business address, business phone, bar
admission — all professionally published. No personal email, home
address, etc. Store only what we need for the database.

## 4. One Suggested scrape strategy: state → city → attorney → firm

> **This is a suggested approach, not a requirement. Also look in to using the sitemap** 
> It mirrors how a
> human browses the directory and gives us a deterministic, resumable
> crawl plan with no URL-shape guesswork. All four levels are
> server-rendered HTML on robots.txt-allowed paths (§3.1), so no JS
> execution is needed.
>
> That said, if the scraper finds a cheaper, faster, or more reliable
> path to the same data — JSON-LD on a different page, an internal JSON
> endpoint the front end uses, the sitemaps in §4.5, anything else —
> it's free to use that instead. The field mapping in §8 is the
> contract; how we reach those fields is flexible.

### 4.1 Level 1 — state index

**URL:** `https://www.martindale.com/find-attorneys/`

This page contains a `BROWSE BY STATES` block. Each state is a link
inside a `<ul class="all-list browse-list__ul">`:

```html
<h2 class="browse-list__h3">Browse by States</h2>
<ul class="all-list browse-list__ul">
  <li class="browse-list__li">
    <a class="navigable browse-list__a--grey"
       href="https://www.martindale.com/by-location/alabama-lawyers/"
       title="Find an Attorney in Alabama">Alabama</a>
  </li>
  ...
</ul>
```

**Suggested selector (CSS):**
`h2.browse-list__h3:contains("Browse by States") ~ ul.browse-list__ul a.browse-list__a--grey`

Equivalently in BeautifulSoup: find the `<h2>` with text `Browse by
States`, take its sibling `<ul>`, then every `<a class*=browse-list__a>`
inside. Yield `(state_name, state_url)` tuples. A common pattern is to
snapshot the HTML to `data/raw/martindale/_index/find-attorneys.html`
first and parse from disk so the parser can be iterated on without
re-fetching.

> **CONFIRMED 2026-05-29 (recon).** selectolax does not support
> `:contains()`. A class-only selector (`ul.browse-list__ul
> a.browse-list__a--grey`) returns ~109 anchors — the page has three
> sections sharing that class: "Browse by States", "Browse by Areas
> of Law" (`/areas-of-law/*`), and "Browse by Cities" (`/all-lawyers/*`).
> The pragmatic fix is to filter anchors by URL prefix: keep only
> hrefs whose path starts with `/by-location/`. That yields **65
> links**, which is ~50 US states + DC + Canadian provinces /
> territories (also under `/by-location/`). For the AZ Bar-parity
> US pilot, filter to a hardcoded US-state name list. Fixtures
> committed at `tests/fixtures/martindale/recon/state_index.json`.

**Sanity check:** we expect ~50 states + District Of Columbia + San Luis Potosi (ignore that one, it is recursive). If the result is materially smaller (e.g. fewer than 45 links), the page
structure has probably changed and it's worth pausing the run to
re-examine.

### 4.2 Level 2 — city index per state

**URL:** `https://www.martindale.com/by-location/{state}-lawyers/`
(e.g. `.../alabama-lawyers/`)

The page renders cities grouped under alphabet tabs (`A`, `B`, `C`, ...).
Each tab is a panel like:

```html
<div class="content abc-panel detail row lists animated fadeIn active"
     id="PanelA" role="tabpanel">
  <div class="content-list-abc">
    <h3 class="all-aop__initial hide-for-small-only">A</h3>
    <ul class="all-list browse-list__ul">
      <li class="browse-list__li"><a href="...">Abbeville</a></li>
      <li class="browse-list__li"><a href="...">Adamsville</a></li>
      ...
    </ul>
  </div>
</div>
```

All 26 alphabet panels (`#PanelA` … `#PanelZ`) are present in the
initial HTML — the tabs are CSS / JS visibility toggles, not lazy
loads. One fetch per state harvests every city link in a single pass.
Note that some states do not have all 26 letters.

**Suggested selector:**
`div[id^="Panel"] ul.browse-list__ul li.browse-list__li a`

Yield `(state, city_name, city_url)` tuples. Useful to snapshot to
`data/raw/martindale/_index/{state}.html`.

**Sanity checks worth running:**
* at least one city link.
* every yielded URL matches `^https://www\.martindale\.com/all-lawyers/[^/]+/[^/]+/$`.

### 4.3 Level 3 — attorney cards per city

**URL:** `https://www.martindale.com/all-lawyers/{city}/{state}/`
(e.g. `.../all-lawyers/abbeville/alabama/`)

Page header is `<CITY> ATTORNEY RESULTS (N)` where `N` is the total
count for that city. Each attorney is a `card card--attorney` block.
Within the card, the fields we care about live in a `<ul>` of
`detail_*` `<li>`s:

```html
<li class="detail_title">
  <a href="https://www.martindale.com/attorney/r-cliff-mendheim-2400451/"
     title="R. Cliff Mendheim - Attorney in Dothan, AL"
     class="opt-d-title subscriber-cpp sortBucketMdc-100"
     data-gtm-tracking='{"profile_type":"Subscriber",
                         "entity_type":"attorney",
                         "entity_name":"R. Cliff Mendheim",
                         "cta_position":"info-section"}'>
    <h3>R. Cliff Mendheim </h3>
  </a>
</li>

<li class="detail_position">
  " Managing Partner at "
  <a class="detail_position--office-link"
     title="Prim & Mendheim, LLC - Law Firm in Dothan, AL"
     href="https://www.martindale.com/organization/prim-mendheim-llc-24659303/dothan-alabama-38318554-f/"
     data-gtm-tracking='{"profile_type":"Subscriber",
                         "firm_id":"38318554",
                         "entity_type":"firm",
                         "entity_name":"Prim & Mendheim, LLC",
                         "cta_position":"affil-firm"}'>
    Prim & Mendheim, LLC
  </a>
</li>

<li class="detail_location noPadding">...</li>
<li class="detail_trophy-awards ...">...</li>
<li class="detail_bio">...</li>
<li class="detail_reviews ...">...</li>
```

Contact CTA (for phone / contact-form):
```html
<a class="button contact"
   href="https://www.martindale.com/attorney/r-cliff-mendheim-2400451/#contactForm"
   title="Contact - R. Cliff Mendheim - Attorney in Dothan, AL">
```

**Suggested extraction per card:**

| Field                | Source                                                                                   |
|----------------------|------------------------------------------------------------------------------------------|
| `attorney_name`      | `li.detail_title > a > h3` text (strip whitespace)                                       |
| `attorney_profile_url` | `li.detail_title > a` `href`                                                           |
| `source_attorney_id` | last numeric segment of `attorney_profile_url`, e.g. `2400451`                           |
| `title_raw`          | `li.detail_position` direct text node *before* the `<a>` — e.g. `"Managing Partner at "` — trim and strip trailing `at` |
| `firm_name_raw`      | `li.detail_position > a.detail_position--office-link` text                                |
| `firm_profile_url`   | `li.detail_position > a.detail_position--office-link` `href` — this is the entry point for §4.4 |
| `source_firm_id_martindale` | `firm_id` field inside `data-gtm-tracking` JSON on the firm `<a>` (e.g. `38318554`) |
| `city`               | from the URL path segment (already known at queue time)                                  |
| `state`              | from the URL path segment (already known at queue time)                                  |
| `street_address`     | `li.detail_location` text (parse city/state/zip out)                                     |
| `phone`              | the `tel:` link near the contact card, or the displayed `833-…` text                      |
| `attorney_website_cta_url` | the card's "WEBSITE" button `href` if rendered — sometimes present on subscriber cards; can short-circuit §4.4 when populated |
| `bio_snippet`        | `li.detail_bio` text (handy for taxonomy harvesting, §5.2)                               |
| `award_count`        | `li.detail_trophy-awards` text, e.g. `"1 Award"` → `1`                                   |

**Pagination on city pages:** if `(N)` in the header exceeds the count
of cards rendered on page 1, walking `?page=2`, `?page=3`, ... until a
page returns 0 cards is the simplest approach. (`?page=` isn't caught
by the `?params=` robots deny-list.)

> **CONFIRMED 2026-05-29 (pagination fix).** Three signals on every
> city page; pipeline uses all three:
>
> 1. `.results__total` span — declared total count
>    (e.g. `<span class="results__total">(6,994)</span>`). Soft
>    sanity baseline only — Martindale's declared totals are
>    inflated (Birmingham declares 6,994 but only 167 pages × 30
>    cards = 5,010 actual). Don't derive page count from this.
> 2. `input.goToPage[data-max]` — the **deterministic** total page
>    count (e.g. `data-max="167"`). Source of truth.
> 3. `a.arrow[rel="next"]` with class NOT `unavailable` — per-page
>    "is there another page" check. Defensive fallback when
>    `goToPage` parsing fails.
>
> Implementation: `parsers/martindale.extract_page_meta()` returns
> `{results_total, last_page, has_next, card_count}`. The pipeline
> logs `martindale.city_meta` on page 1, walks to `last_page`
> (capped by `--max-pages-per-city`), and warns
> `martindale.count_mismatch` if scraped cards diverge from
> `results_total` by more than ~50% (only when the full walk was
> attempted).
>
> **Hard cap at 167 pages.** Phoenix declares `results_total=17,522`
> but `data-max=167`, the same as Birmingham. Martindale apparently
> caps page enumeration at 167 regardless of declared total — so for
> very large cities we can access at most 167 × ~30 = ~5,010
> attorneys via this route. For full coverage of mega-cities we'd
> need to filter (state+practice area, alphabetical letter, etc.)
> to reduce the per-query result set under the cap. Out of scope
> for the pilot; tracked here as a known limitation.

**Resumability:** treating `(state, city)` pairs from §4.2 as the work
queue and persisting completed pairs to a small SQLite table
(e.g. `martindale_city_runs`) lets a crashed run resume mid-state
without re-walking what's already done.

### 4.4 Level 4 — firm profile (optional, for firm website + office details)

URL pattern (taken from `firm_profile_url` on the attorney card, never
hand-built):

`https://www.martindale.com/organization/{firm-slug}-{firm-id}/{city-slug}-{state}-{location-id}-f/`

e.g.
`https://www.martindale.com/organization/prim-mendheim-llc-24659303/dothan-alabama-38318554-f/`

The page has a sticky masthead with three CTAs — phone, **CONTACT**,
**WEBSITE** — then an "About our {city}, {state} office" blurb and an
"Office Details" block with mailing address and office size. The
**WEBSITE button is what we're after** — it's the canonical link to
the firm's own site, which is the single most valuable enrichment
field per firm:

```html
<a href="http://www.pm-firm.com"
   target="_blank"
   rel="sponsored"
   class="webstats-website-click button profile-website-body"
   data-gtm-event="pub_website_click"
   data-gtm-tracking='{"profile_type":"Subscriber","entity_type":"office",
                       "entity_name":"Prim & Mendheim, LLC",
                       "location_name":"Dothan, AL",
                       "cta_position":"masthead"}'>
```

**Suggested selector:** `a.profile-website-body[href]`.

> **CONFIRMED 2026-05-29 (recon).** That class did NOT match on a
> sample firm (Prim & Mendheim, `firm_id=38318554`) — but the
> equivalent button exists under a different class:
> `a.webstats-website-click[href]` (GTM event
> `data-gtm-event="pub_website_click"`). The recon and parser use
> `a.webstats-website-click[href], a.profile-website-body[href]` —
> both selectors, in that order, so we survive A/B styling. The
> captured value is `http://www.pm-firm.com` with `rel="sponsored"`.
> Fixture: `tests/fixtures/martindale/recon/sample_firm_profile.json`.

**Suggested run shape:**

1. Collect the set of unique `firm_profile_url` values from the
   attorney pass (§4.3). Dedup — a 30-attorney firm should resolve to
   a single firm fetch, not thirty.
2. For each unique firm URL, fetch the page once and extract:

| Field                          | Source                                                       |
|--------------------------------|--------------------------------------------------------------|
| `firm_website_url`             | `a.profile-website-body[href]`                               |
| `firm_website_is_sponsored`    | `true` if that `<a>` has `rel="sponsored"`                   |
| `firm_mailing_address`         | "Office Details → Mailing Address" text                      |
| `firm_office_blurb`            | "About our {city}, {state} office" body text                 |
| `firm_phone`                   | masthead phone CTA text                                      |

3. Persist on a new `MartindaleFirmProfile` row keyed on
   `firm_profile_url` (or on `source_firm_id_martindale`) and join
   into `FirmSourceRecord` at aggregation time.

**Shortcut worth considering:** if `attorney_website_cta_url` is
already populated on the attorney card (§4.3), that's typically the
same URL as the firm-profile WEBSITE button. The scraper can prefer
the card value and skip the firm-profile fetch when it's present —
falling back to Level 4 only when the card didn't expose a website.
This roughly halves the firm-level request budget.

**Caveats:**

* Not every firm shows a WEBSITE button. Basic (non-Subscriber)
  profiles often have only phone/contact. `firm_website_url` is
  optional — record it when present, leave it null otherwise.
* `rel="sponsored"` is Martindale flagging the link as paid placement.
  It's still the canonical firm URL most of the time, so we capture
  it.
* Adding Level 4 inline roughly doubles the request budget. Running
  it as a deferred second pass (after the attorney pass finishes)
  makes Cloudflare backoff easier to recover from and lets the firm
  pass dedup before any fetching happens.

### 4.5 Sitemaps (alternative URL source)

robots.txt also advertises five sitemaps — `sitemap_profiles.xml`,
`sitemap_new_profiles.xml`, `sitemap_browse.xml`,
`location_sitemap_browse.xml`, `areas_sitemap_browse.xml`. The suggested
strategy above doesn't lean on them, mostly because the geographic
hierarchy can be more useful than a flat profile list, but they're a fully
legitimate alternative if the scraper prefers them. They're also
useful as-is for:

* **Delta runs** later — `sitemap_new_profiles.xml` to refresh recent
  additions without re-walking every state.
* **QA** — diff our crawled attorney count against the sitemap's
  attorney count per state to spot missing cities.

## 5. Title harvesting & canonical-title taxonomy (suggested implementation)

**OPTIONAL / SUGGESTED**
The following section (5) is a *suggested* implementation for titles.
This method of implementation is completely optional. 
If the program prefers a different way of going about 
creating the hierarchy, please feel free to use that instead.

Martindale exposes a freeform **position title** for each attorney (the
text in `li.detail_position` before the firm link, e.g. `"Managing
Partner at"`, `"Attorney at"`, `"Partner at"`, `"Of Counsel at"`,
`"Founder at"`). These titles are not in our taxonomy today, and they
matter for firm-level signals (a "Managing Partner" is a much stronger
firm-affiliation signal than a generic "Attorney").

The goal here is for the scraper to capture every `title_raw` it sees
and feed an unmatched-titles report, the same shape as the unmatched
practice-areas report from M4 — so that titles can become part of the
canonical taxonomy over time.

### 5.1 Per-attorney behavior (suggested)

1. Extract `title_raw` from `li.detail_position` (strip the trailing
   `" at"` and any whitespace).
2. Normalize: lowercase, collapse internal whitespace, strip punctuation.
3. Look up against the previously found titles. If
   matched, store the canonical title on the record. If not, store the
   raw form and log it to the unmatched-titles report.

### 5.2 Per-run output: `unmatched_titles_<run-ts>.csv` (suggested)

Suggested columns: `title_raw`, `normalized`, `occurrence_count`,
`example_attorney_url`. Sorting descending by `occurrence_count` makes
review easy. After the pilot, the workflow is: Alex reviews the file
and decides which titles to promote into `config/titles.yaml` and what
to map them to.

### 5.3 `config/titles.yaml` — canonical title file (suggested)

Same structure as `practice_areas.yaml`:

```yaml
- canonical: managing_partner
  aliases:
    - "managing partner"
    - "managing member"
    - "managing shareholder"
- canonical: partner
  aliases:
    - "partner"
    - "equity partner"
    - "name partner"
- canonical: of_counsel
  aliases:
    - "of counsel"
    - "senior counsel"
- canonical: associate
  aliases:
    - "associate"
    - "senior associate"
- canonical: attorney
  aliases:
    - "attorney"
    - "attorney-at-law"
    - "lawyer"
- canonical: founder
  aliases:
    - "founder"
    - "founding partner"
    - "founding member"
```

Seed this file with the obvious candidates above. The pilot's unmatched-
titles report will surface the long tail — particularly senior /
specialized titles (`General Counsel`, `Chair`, `Practice Group Leader`,
etc.) that we add as we find them. Treat the yaml as living
documentation, the same way `practice_areas.yaml` is.

### 5.4 Where titles could land in the canonical schema (suggested)

Add a `title_canonical` column on `FirmPerson` (M6 work — for now
just persist `title_raw` and `title_canonical` on `FirmSourceRecord`).
At resolution time, conflicting titles for the same `(person, firm)`
across sources are resolved by: prefer most-senior title in the
canonical ordering `managing_partner > founder > partner > of_counsel
> associate > attorney`.

## 6. Pagination & dedup

* Walk `?page=1..N` until a page returns 0 `LegalService` items in the
  JSON-LD `@graph`.
* **Adjacent pages may overlap ~15–20%.** Dedup inside a run by the
  Martindale numeric id parsed from `@id` (regex: `/(\d+)/?$`).
* `source_firm_id` for `FirmSourceRecord` stays our existing
  `normalize(name) + normalize(streetAddress)` key — Martindale id is
  per-attorney, not per-firm. Confirm this with a regression test before
  the full sweep, same as we did for AZ Bar.

## 7. Rate limit, retries, caching

Mirror AZ Bar's polite-ramp config:

| Knob                 | Pilot value                                                         |
|----------------------|---------------------------------------------------------------------|
| `rate_limit_rps`     | 0.5 (starts off slower than AZ Bar Cloudflare, speed up after)      |
| `request_timeout_s`  | 30                                                                  |
| Backoff on 429/503   | Exponential, base 2s, max 60s, 5 attempts, then **abort the run**   |
| Cloudflare challenge | Treat as a block for now. Stop, log the page, don't try a new IP yet|
| Cache                | Write raw HTML to `data/raw/martindale/<state>/page-<n>.html` first |

The "scrape raw to disk, then parse from disk" pattern means we can iterate
on the parser without re-fetching.

## 8. Field mapping to our canonical schema

This table is the contract — these are the fields we want, regardless
of which traversal the scraper takes. Sources below assume the §4
approach; substitute equivalents from sitemaps / JSON-LD / other paths
if the scraper picks a different route.

**Per-attorney fields** (from the city-page attorney card, §4.3):

| Our field (FirmSourceRecord)         | Martindale source on the card                                            |
|--------------------------------------|--------------------------------------------------------------------------|
| `source`                             | constant `"martindale"`                                                  |
| `source_attorney_id`                 | trailing numeric segment of `li.detail_title > a` `href` (e.g. `2400451`) |
| `source_attorney_url`                | `li.detail_title > a` `href`                                             |
| `attorney_name`                      | `li.detail_title > a > h3` text                                          |
| `title_raw`                          | `li.detail_position` text before the firm `<a>` (trim, drop trailing `" at"`) |
| `title_canonical`                    | lookup of `title_raw` in `config/titles.yaml` (§5)                       |
| `firm_name_raw`                      | `li.detail_position > a.detail_position--office-link` text (or `__solo__` if no `<a>`) |
| `source_firm_id_martindale`          | `firm_id` parsed from `data-gtm-tracking` JSON on the firm `<a>`         |
| `firm_profile_url`                   | `li.detail_position > a.detail_position--office-link` `href`             |
| `street_address`                     | `li.detail_location` parsed                                              |
| `city`                               | from URL path (already known at queue time)                              |
| `state`                              | from URL path (already known at queue time)                              |
| `phone`                              | contact card `tel:` link text                                            |
| `image_url`                          | attorney headshot `<img src>` inside the card                            |
| `bio_snippet`                        | `li.detail_bio` text                                                     |
| `award_count`                        | `li.detail_trophy-awards` text → int                                     |
| `scraped_at`                         | run timestamp                                                            |

**Per-firm fields** (from the firm profile page, §4.4):

| Our field (MartindaleFirmProfile)    | Martindale source on the firm page                                       |
|--------------------------------------|--------------------------------------------------------------------------|
| `source_firm_id_martindale`          | from the attorney card's `data-gtm-tracking`, or the firm URL slug       |
| `firm_profile_url`                   | the URL itself                                                           |
| `firm_website_url`                   | `a.profile-website-body[href]` (the masthead WEBSITE button)             |
| `firm_website_is_sponsored`          | `true` iff that same `<a>` has `rel="sponsored"`                         |
| `firm_mailing_address`               | "Office Details → Mailing Address" text                                  |
| `firm_office_blurb`                  | "About our {city}, {state} office" body text                             |
| `firm_phone`                         | masthead phone CTA text                                                  |
| `firm_scraped_at`                    | run timestamp                                                            |

Our existing dedup key (`normalize(firm_name) + normalize(street)`)
still drives `source_firm_id` on `FirmSourceRecord`. The new
`source_firm_id_martindale` is an additional column — it lets us
later link multiple attorneys at the same Martindale firm even when
their street addresses differ slightly (branch offices), without
breaking the AZ Bar dedup key. `firm_website_url` joins onto
`FirmSourceRecord` via `source_firm_id_martindale`.

## 9. Pilot scope recommendation

Goals of the M5 pilot: **exercise firm aggregation at scale**, **seed
the title taxonomy** (§5), and **capture firm websites** for downstream
enrichment — none of which AZ Bar got us.

Suggested run plan (assumes the §4 traversal; adapt if the scraper
takes a different route):

1. **Level 1.** Fetch `/find-attorneys/`. Extract 50+ state links per
   §4.1. (1 request.)
2. **Filter to one state for the pilot.** Alabama is a good first
   choice — different state and different alphabet skew vs AZ Bar,
   and small cities like Abbeville already show multi-attorney firms
   (e.g. Prim & Mendheim) which is exactly what we need to exercise
   aggregation. Alternatively, feel free to ues cities in Arizona to 
   see the comparison with AZ Bar.
3. **Level 2.** Fetch `/by-location/alabama-lawyers/`. Harvest every
   city link from `#PanelA` … `#PanelZ` per §4.2. (1 request.)
4. **Level 3.** For the pilot, a reasonable cap is the **first 10
   cities alphabetically** plus **Birmingham, Mobile, Montgomery,
   Huntsville, Tuscaloosa** so a few large cities are represented.
   For each, fetch the city page and any `?page=N` continuations
   until cards run out. Rough estimate: ~15 cities × ~2 pages avg =
   ~30 requests, yielding 200–400 attorney records.
5. **Level 4 (firm pass).** Dedup the `firm_profile_url` set from
   step 5 and fetch each firm page once (§4.4). Expect significant
   dedup — 200–400 attorneys typically resolves to ~80–150 unique
   firms. If `attorney_website_cta_url` was populated on the card,
   the firm fetch can be skipped for that firm. Estimate: ~80
   additional requests.
6. **Parse + persist.** Attorney rows → `FirmSourceRecord` with
   `source = "martindale"`. Firm rows → `MartindaleFirmProfile`,
   joined by `source_firm_id_martindale`.
7. **Run the existing name+street regression test** against the
   resulting records. We expect it to pass unchanged.
8. **Emit reports:**
   - `unmatched_titles_<run-ts>.csv` per §5.2 — review and promote
     entries into `config/titles.yaml`.
   - Firm-count histogram (attorneys per
     `source_firm_id_martindale`) — this is the firm-aggregation
     signal we couldn't get from AZ Bar's solo skew.
   - Firm-website coverage rate — what fraction of unique firms
     yielded a `firm_website_url`, and what fraction of those were
     `is_sponsored=true`.

**Total request budget:** roughly ~115 requests. At 0.5 rps that's
~4 minutes — still well under the AZ Bar pilot, with substantially
richer data per firm.

After the pilot, the full sweep is roughly all 50 states × ~200
cities × ~2 pages avg ≈ 20k attorney requests + ~5–10k firm requests.
We will increase the rps for that run. Same nod-before-full-sweep posture as
AZ Bar.

## 10. Known pitfalls

* **Cloudflare.** First sign of trouble — stop. Don't escalate tooling yet (leave for later).
* **City-page card order is sponsor-weighted** (`sortBucketMdc-100`
  class on the Abbeville example is a hint). Page-1 cards are not a
  random sample; note this in any "% of firms with X" stat.
* **Subscribers vs basic profiles render differently.** Premium
  (`Subscriber`) cards have all the `detail_*` `<li>`s; basic profiles
  may omit `detail_position`, `detail_bio`, `detail_trophy-awards`.
  Treat every `detail_*` field as optional and avoid hard-failing on a
  missing one — logging the variant as a `card_shape` value is
  enough. Same applies to firm-profile pages: not every firm exposes a
  WEBSITE button or office blurb.
* **`li.detail_position` may have NO `<a>`** for solo practitioners
  whose firm name is just their own name. In that case the entire
  position text is freeform (e.g. `"Solo Practitioner"`). Treating
  these as `__solo__` per the AZ Bar convention is the safe default.
* **`data-gtm-tracking` is a JSON string inside an HTML attribute.**
  Unescape HTML entities (`&quot;` → `"`) before `json.loads`. It's
  worth a try/except — the attribute isn't always parseable, and the
  visible text is a fine fallback.
* **`rel="sponsored"` on firm WEBSITE links** is Martindale tagging
  paid placement. The URL is still the canonical firm site in most
  cases.
* **Same attorney listed in multiple cities** (multi-office partners).
  Within a run, dedup by `source_attorney_id`. Across runs, use
  `source + source_attorney_id`.
* **City page counts can be tiny.** Some Alabama cities have 1–2
  attorneys, some have hundreds. Don't pre-allocate budgets per city —
  use the header `(N)` to know when to stop paginating.
* **Title harvesting is open-ended.** Expect the unmatched-titles
  report to be 50–100 entries from the first pilot — the long tail
  (`Practice Group Leader`, `Chief Marketing Officer`, etc.) is real.
  Don't try to pre-populate every alias.

## 11. Potential open questions for the next check-in

* For `config/titles.yaml`: is `title_canonical` enough, or do we also
  want a `seniority_rank` int so M6 resolution can pick the most-
  senior title without re-encoding the ordering in code?
* Do we want per-state run logs (state = run unit) or one big run log
  per sweep?
* Storage: same SQLite as AZ Bar, or a per-source raw cache table from
  M5 onward?
* When the full sweep starts: do we want a checkpoint commit per
  state, so a mid-sweep failure leaves a clean partial dataset?
