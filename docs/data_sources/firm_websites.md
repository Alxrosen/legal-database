# Data source: law firm websites

Status: **planned (M7)** — enrichment source after
Owner: Alex
Last reviewed: 2026-05-29

## Overview

The following are a list of **suggestions** in place to help you create a 
better scraper and parser for the by-firm-website enrichment level. They are
not to be taken as definitive instructions, but rather interesting considerations
that shoud prompt additional recon, planning, and edge-case-considerations. 

Do *NOT* feel obligated to follow any of these if they are not idiomatic, 
not ideal, or don't fit the current schema.

## 0. Tracking firm-website enrichment (suggested)

I suggest keeping track of what firms have been enriched through the use of
their website. This makes it so that, as we add new firms and sources, we 
can check which ones have already been enriched without having to 
rescrape ones that have recently been enriched. This can be done as a
timestamp where NULL means it has never been enriched (or cannot be 
enriched if there is no website attached to it). This is a suggested
implementation but I leave the actual one up to you. 

An important future considerations might be how to resolve different 
websites, which will depend on when the enrichment takes place. Please
think ahead about what the idiomatic and best way of doing this might be. 

## 1. What this source is (suggested)

For some of the firms we resolve from the directories (AZ Bar, Martindale,
etc.) we have a `firm_website_url`. This step visits each of those
URLs and tries to extract:

* a canonical firm description (what they do, who they serve),
* attorney headcount (e.g. `40+`, `475`, `1`),
* office count and locations,
* years in operation / founded date,
* size, revenue, or how established it is
* notable signals (Fortune 500 clients, billions recovered, awards),
* a sanity check that the URL actually belongs to the firm.

NOTE many of these are trying to serve as proxies for EBITDA. If you
find other symbols that might be better (e.g. revenue), include those
as well. 

This source is the **messiest** of the three. Directory data is
structured. Firm websites are 50,000 different bespoke marketing sites
on a handful of templating platforms, with no shared schema. The
playbook below is built around helping you to consider some edge cases.
It is NOT EXHAUSATIVE; you will have to fetch many more and continue to 
do research on what the best, most idiomatic, and most effective way of 
parsing these websites will be. 

## 2. The biggest gotcha: there is no single right page (suggested)

In all likelihood, the best way of doing this is to try and find 
the sitemap or using the NAV. However, the following is an example
of what might happen if edge cases are not considered. 

The first instinct might be to "fetch `/about` and parse it." 
This might work often but can still be dangerous.

| Firm                  | "About page" actually lives at        | Notes                                       |
|-----------------------|---------------------------------------|---------------------------------------------|
| Buchanan Ingersoll    | `bipc.com/about-buchanan`             | Domain is initialism; real name is Buchanan |
| Goetz Law Group   | `Goetzlaw.com/about`              | Standard path, but data shape is unusual    |
| The Estate Planning Law Group | `teplg.com/our-team`          | No `/about`; team page is the closest equivalent |
| kdlaw P.C.            | `kdlaw.org/our-attorney/`             | No `/about`; solo, single-attorney page     |
| Max Draitser firm     | `bikelawla.com/our-firm/`             | "Our firm" rather than "about"              |
| The Entrekin Law Firm | `lanceentrekin.com/about/`            | Path is `/about/` (trailing slash matters)  |

**Suggested page-discovery order** (try each in order, accept the
first that loads with a 200 and produces extractable content):

1. `/about`, `/about/`, `/about-us`, `/about-us/`
2. `/about-{firm-initial-or-name}` — derive from domain (`bipc.com` →
   `/about-buchanan` is hard to guess; **fall back to crawling the nav**
   when the obvious URLs miss)
3. `/our-firm`, `/our-firm/`, `/firm`, `/the-firm`
4. `/our-team`, `/team`, `/our-people`, `/people`
5. `/attorneys`, `/our-attorneys`, `/lawyers`, `/our-attorney` (singular
   variant is a strong solo-practice signal)
6. Home page (`/`) — many small firms put the whole story on the home
   page

**The reliable fallback:** fetch the home page, parse the top nav
(usually `<nav>`, `<header>`, or first `<ul>` with link items), and
collect every internal link whose anchor text matches `about|firm|team|
people|attorneys|lawyers|our story|who we are`. Try those in order.
Cache the discovered `(domain → about_page_url)` mapping so future
runs can skip the discovery step.

## 3. Headcount extraction: BIPC vs Goetz case studies (suggested)

Headcount is the single most valuable field and the one with the most
varied presentation. Two extreme examples from our sample:

### 3.1 Buchanan (easy mode: prose paragraph) 

```
Buchanan Ingersoll & Rooney is a national law firm with a proven
reputation for providing progressive, industry-leading legal, business,
regulatory and government relations advice to our regional, national
and international clients. Our 450 attorneys and government relations
professionals across 16 offices proudly represent some of the highest
profile and innovative companies in the nation, including 50 of the
Fortune 100.
```

A regex that catches `(\d+)\s+attorneys?` over the body text gets us
`450 attorneys`. A regex for `(\d+)\s+offices?` gets us `16 offices`.
Bonus signals we can lift from the same paragraph:

* `national law firm` → `scope: national`
* `Fortune 100` / `Fortune 500` → `notable_clients_tier`
* `regulatory and government relations` → practice-area hints

**Caveat:** "Our 450 attorneys **and government relations
professionals**" is ambiguous — the 450 might include non-lawyers.
Prefer the more specific phrasing `(\d+)\s+(attorneys|lawyers)\b`
**not** followed by `and ... professionals`. When ambiguous, store
the raw quote alongside the parsed count so a human can audit.

Furthermore, note that the regex might have to check multiple ways
of saying the same thing, such as "number of lawyers: 450." (This 
case is unlikely, but it highlights the importance of having multiple
regexes, though controlled enough that they do not accidentally get
incorrect information.)

Note: the live page when we fetched said `450 attorneys`; the user's
notes referenced `475 attorneys`. These pages change frequently —
**always store the snapshot date** with the extracted figure.

### 3.2 Goetz (hard mode: big-number H2s)

The same information is split across multiple H2s, each with a short
descriptive sentence:

```html
## Billions
We have recovered billions of dollars for clients ...

## 600+ Staff
With three fully-staffed locations, our team of truck accident
professionals is here to support you and your case.

## 25+ Years
Service to our community and clients spans over two decades.

## 40+ Lawyers
We have a deep bench of industry leading attorneys ...
```

The visual hierarchy on the page is huge type — `25+`, `40+`, `Years`,
`Lawyers` on separate lines. In the DOM these are H2 nodes.

**Suggested extraction:** iterate every H1/H2/H3 on the page, match
each against a small set of patterns:

| Pattern                                                  | Field                | Example match       |
|----------------------------------------------------------|----------------------|---------------------|
| `\b(\d+)\s*\+?\s*(years?|yrs?)\b` (text or adjacent H2)  | `years_in_operation` | `25+ Years`         |
| `\b(\d+)\s*\+?\s*(lawyers?|attorneys?)\b`                | `attorney_count`     | `40+ Lawyers`       |
| `\b(\d+)\s*\+?\s*(staff|employees|professionals)\b`      | `staff_count`        | `600+ Staff`        |
| `\b(\d+)\s*\+?\s*(offices?|locations?)\b`                | `office_count`       | `3 fully-staffed locations` (the 3 is upstream of the noun, handle generically) |
| `\b(billions?|millions?)\b.*\b(recovered|settlements?|verdicts?)\b` | `recovery_blurb` | `recovered billions of dollars` |

The `+` suffix means "at least" — store as `attorney_count_min: 40`
(int) plus `attorney_count_is_minimum: true` (bool). Don't drop the
`+`; it changes the semantics.

This is **just a suggestion**, please implement it based on whatever 
you notice works best. 

**Edge case in this same page:** the page says `three fully-staffed
locations` in the 600+ Staff section, but the footer lists **four**
offices (Dallas, Fort Worth, Atlanta, Chicago). When two numbers on the
same site disagree, the footer addresses are the more reliable source.
Generally: addresses with phone numbers > marketing copy headline
numbers. 

Also if the firm website lists a phone number, add it to a list of phone
numbers we already have. If it is the same, you can disregard it, but
dont throw away the old phone number -- add it. This can be stored as a 
JSON blob or a list as we will not query by phone numbers, but make sure that
it is still easy to use phone numbers (even a list) as a deduping technique. 

## 4. Team-page extraction: counting attorneys vs staff (suggested)

When the firm doesn't volunteer an attorney count, you can also 
count the team listing yourself. The pattern from TEPLG (`teplg.com/our-team`):

```
## Bill Deitch          Attorney & Counselor at Law   (with bio)
## Kirsten Izatt        Attorney & Counselor at Law   (with bio)
## Kathleen DiCola      Attorney & Counselor at Law   (Of Counsel, with bio)
## Dawn Neumann         Administrative Assistant      (small image only)
## Kristen Oakley       Paralegal
## Stephanie Rath       Trust & Estate Coordinator
## Dianna Weglarz       Senior Paralegal
```

**Suggested approach:** for each H2/H3 on a team page, look for an
adjacent role title and classify:

| Role text contains                                            | Bucket           |
|---------------------------------------------------------------|------------------|
| `attorney`, `lawyer`, `counselor at law`, `esq`, `partner`, `associate`, `of counsel` | `attorneys` |
| `paralegal`, `legal assistant`, `assistant`, `coordinator`, `secretary`, `office manager` | `staff` |
| `founder`, `managing partner`, `principal` (without other context) | `attorneys` (with title flag) |

For TEPLG that yields: **3 attorneys** (Bill, Kirsten, Kathleen — one
flagged "Of Counsel") and **4 staff** (Dawn, Kristen, Stephanie,
Dianna). Total team = 7.

**Solo-practice signals** (any one of these is strong; two together is
near-certain):

* The nav has a singular "Our Attorney" or "Meet Our Attorney" link.
* The team/about page lists exactly one attorney bio.
* The about-page copy uses first-person singular: `I am ...`, `my
  practice`, `my clients`, `the founding attorney of ...`.
* The phone number on the contact page is a personal mobile-pattern
  number rather than a switchboard.

KD Law and the Draitser firm both trip every one of those.

## 5. Garbage / mismatched-URL detection (suggested)

The Ellingson example: directory data has "Ellingson Law Group LLC"
with a website that points to `swissbiologic.com` (a Swiss dental
products company). The eBay example: "eBay" somehow tagged as a law
firm. The Draitser example: `bikelawla.com` is a perfectly real firm
URL even though "Draitser" appears nowhere in the domain.

The check needs to handle all three.

### 5.1 Suggested decision tree

For each `(firm_name, firm_website_url)` pair:

1. **Fetch the homepage and the title/H1.** If the fetch fails after
   reasonable retries, flag as `url_unreachable` and move on.
2. **Is the page legal-related at all?** Look at:
   * `<title>` / `<meta description>` / first H1 / first 500 words
     of body text
   * tokens: `law`, `attorney`, `legal`, `lawyer`, `counsel`,
     `p.c.`, `llp`, `pllc`, `apc`, `esquire`, `practice`,
     `bar association`
   * a hit on at least 2 of those tokens (or 1 prominent one in
     `<title>`) → "legal".
   * **No legal tokens at all** → flag `url_not_a_law_firm` and bail
     out. (This catches `swissbiologic.com`, the eBay case, etc.)
3. **Does the page match this firm specifically?** Compute a similarity
   score between `firm_name` and the page's identity. Use *all* of:
   * Normalized direct match (lowercase, strip `LLC`/`P.C.`/`LLP`/`PLLC`/`APC`/
     `&`/punctuation): `ellingson law group` vs the page's H1/title.
   * Initial match: `BIPC` vs `Buchanan Ingersoll & Rooney` → take the
     initial letters of each word in the page's H1 (`B`, `I`, `&`, `R`
     → `BIR` or `BIPC` with `P.C.` suffix). If the firm name *is* the
     initials of the page's H1, it's a match.
   * Domain-token match: `bikelawla` → `bike law la` → check if any
     of those tokens are practice-area or location hints from the
     firm's directory profile (Draitser firm in LA doing bicycle law
     → matches).
   * Founder-name match: if the page's H1/H2 mentions "I am Max
     Draitser" and Max Draitser is the directory's listed attorney,
     it's a match even when the firm name and domain look unrelated.
4. **Resolution:**
   * All three signals positive → `url_verified`.
   * Page is legal but firm-name mismatch → `url_legal_but_mismatched`
     (could be a renamed firm, a merged firm, or directory error).
     Don't drop — human review.
   * Page is not legal → `url_not_a_law_firm`. Confidence-flag.
     This is `swissbiologic.com`.
   * Page is unreachable → `url_unreachable`. Retry on next sweep.
5. **Asymmetric verdicts:** Don't auto-drop. The Ellingson case is
   "directory says it's a real firm but the URL is wrong." The eBay
   case is "the firm name is wrong AND the URL is wrong." These are
   different bugs upstream — the first means refetch the directory's
   website field; the second means the firm row itself is junk.

### 5.2 Suspicious-domain patterns to watch

* Free-subdomain hosts: `*.wix.com`, `*.wixsite.com`,
  `*.squarespace.com`, `*.webflow.io` — usually staging sites, not
  the canonical URL.
* Aggregator profiles, not the firm itself: `martindale.com/...`,
  `findlaw.com/...`, `justia.com/...`, `avvo.com/...`,
  `superlawyers.com/...`. If the directory's `firm_website_url` is
  another directory, recurse one level via the aggregator profile to
  find the real site.
* Marketing-vendor staging domains: e.g. Goetz has
  `wlg-nomos.webflow.io` linked alongside the production
  `Goetzlaw.com`. Treat anything ending in `.webflow.io`,
  `-staging.`, `dev.`, `test.` as non-canonical.
* `.gov` or `.edu` URLs in a "law firm website" field are almost
  certainly mis-mapped (probably a bar-association profile).

## 6. Platform / template fingerprints (suggested)

A surprising amount of law-firm web traffic runs on a handful of
templating platforms. Detecting the platform up front lets the parser
pick a more specific strategy.

| Platform        | Fingerprint                                                                      | Implications                                                  |
|-----------------|----------------------------------------------------------------------------------|---------------------------------------------------------------|
| Wix             | `<meta name="generator" content="Wix.com Website Builder">`, `static.wixstatic.com` image URLs | Predictable: H2 = name, sibling text = title; bio in a sibling `<p>`. TEPLG. |
| Webflow         | `cdn.prod.website-files.com` image URLs, `webflow.io` staging links              | Big-number-H2 pattern common; content blocks are clean. Goetz. |
| Scorpion (legal CMS) | Footer link to `scorpion.co/law-firms`, brand logos at `/images/brand/`     | Standard "Our Attorney(s) / Practice Areas / Reviews / News & Resources" nav. KD Law. |
| Custom / proprietary | None of the above; usually larger firms                                     | Use prose-extraction. BIPC. |
| Findlaw-hosted  | URLs under `findlaw.com/lawfirm/`, vendor footer                                 | These are directory profiles, **not** firm sites. Don't treat as the firm's own website. |

Suggested: probe the homepage once per domain, record the platform, and
cache it in the firm row (`website_platform`). Future runs can short-
circuit to the right parser.

## 7. Fields we want per firm (suggested) 

| Field                     | Type   | Notes                                                              |
|---------------------------|--------|--------------------------------------------------------------------|
| `firm_id`                 | int    | Our internal firm row                                              |
| `website_url`             | str    | Canonical site                                                     |
| `website_resolved_url`    | str    | After redirects                                                    |
| `website_platform`        | str    | `wix`, `webflow`, `scorpion`, `findlaw`, `custom`, `unknown`       |
| `about_page_url`          | str?   | Discovered per §2                                                  |
| `about_page_fetched_at`   | ts     | Snapshot timestamp                                                 |
| `attorney_count_min`      | int?   | Parsed (`40+ lawyers` → 40)                                        |
| `attorney_count_is_min`   | bool   | True if `+` was present                                            |
| `attorney_count_raw`      | str?   | The matched phrase, for audit                                      |
| `staff_count_min`         | int?   | Non-attorney team                                                  |
| `office_count`            | int?   | From copy or footer count                                          |
| `office_addresses`        | list   | Parsed from footer (often the most reliable source)                |
| `years_in_operation_min`  | int?   | From `25+ Years` or `Founded in 1850` → `current_year - 1850`      |
| `description_blurb`       | str?   | First 2–3 sentences from about page (for downstream summarization) |
| `scope`                   | str?   | `national` / `regional` / `state` / `local` — inferred from copy   |
| `notable_signals`         | list   | `Fortune 500`, `recovered billions`, `BTI Client Service A-Team`, etc. |
| `mergers_history`         | list?  | Predecessor firm names (BIPC lists several; useful for matching)   |
| `url_verification_status` | enum   | `verified` / `legal_but_mismatched` / `not_a_law_firm` / `unreachable` |
| `url_verification_score`  | float  | 0–1, used for ranking review queue                                 |

`description_blurb` deliberately stays as raw text — we leave the
generated, prettier "description" field to a downstream summarization
pass (Alex's call: LLM or template).

## 8. Edge cases worth designing for upfront (suggested)

* **Domain says one name, page says another.** BIPC vs Buchanan. The
  page H1/title is authoritative for the firm name; the domain is
  not. Store both: `firm_name_canonical` from the page, `domain` from
  the URL.
* **Firm name is a person.** "The Entrekin Law Firm" vs "Lance
  Entrekin" — the founder's name often appears in the domain
  (`lanceentrekin.com`). Useful signal for solo / boutique
  classification.
* **Firm DBAs.** "Max Draitser" doing business as "Southern California
  Bicycle Attorneys" on `bikelawla.com`. Track all three:
  registered name, DBA, domain.
* **Mergers and predecessor names.** BIPC's about page lists
  "Silverstein and Mullens", "Burns Doane Swecker & Mathis LLP",
  "Klett Rooney Lieber & Schorling", "Fowler White Boggs P.A." as
  predecessors. These are useful for matching historical directory
  records that still use the old names. Capture them.
* **Marketing vs reality discrepancies.** "Three fully-staffed
  locations" in copy vs four addresses in footer (Goetz). When
  multiple numbers on the same site disagree, prefer the more
  concrete source: footer addresses with phones > body copy with big
  numbers > headline marketing claims.
* **Headcount staleness.** Pages don't get updated regularly. The
  BIPC page said `450 attorneys` when we fetched; an earlier note
  had `475`. Always store the fetch timestamp; never overwrite a
  newer figure with an older fetch.
* **Phrase ambiguity.** "Our 450 attorneys and government relations
  professionals" — is the 450 attorneys only, or attorneys +
  professionals? Default to "ambiguous, raw phrase stored", flag for
  review.
* **Years-in-operation math.** "Founded in 1850" + current year =
  176 years. "25+ Years" = at least 25. "Over a quarter century" = at
  least 25. Build a tiny normalizer that accepts all three.
* **Page rendered with JS.** Most law-firm sites are server-rendered,
  but some Wix/Squarespace builds hide content behind JS. If the raw
  HTML body has fewer than ~500 characters of meaningful text, that's
  a hint to retry with headless Chrome. Suggest doing this lazily,
  not by default.
* **Cookies / GDPR walls.** Some firms (especially European-affiliated)
  hide content behind a consent banner that the bot can't dismiss.
  Detect the banner, log it, move on.
* **`robots.txt` and rate limits.** Each firm site has its own
  robots.txt. Honor it. The crawl rate is naturally low (one or two
  pages per firm, thousands of firms) — pace at 0.5 rps per host with
  global concurrency 4–6.
* **Press releases / blog posts mistaken for about copy.** When
  falling back to "first text-heavy page," skip URLs that contain
  `/blog/`, `/news/`, `/press/`, `/insights/`, `/article/`.
* **Image-only "about" sections.** Some firms put their key claims
  inside images ("Over 25 Years" rendered as a logo). The Lance
  Entrekin home page has "25+ years of experience winning big
  results" both as text on the about page AND as a graphic. Prefer
  text; fall back to image-alt-text; don't OCR unless explicitly
  enabled.
* **The "About page is the home page" case.** Solo practitioners
  (Draitser, KD Law, Entrekin) often put everything on the home page
  with no separate /about. Always include the home page as a
  candidate in step §2.

## 9. Suggested pilot (suggested)

Run against a sample of **30 firms from the AZ Bar pilot**, chosen to
cover:

* 5 large national firms (BIPC-shape)
* 5 mid-size regional firms (Goetz-shape)
* 10 small/boutique firms (TEPLG-shape, KD Law-shape)
* 5 solo practitioners (Draitser-shape)
* 5 deliberately broken or suspicious URLs to test §5 detection

Emit per-firm:
* The discovered `about_page_url` (or fail reason)
* All parsed fields from §7
* The verification status

Emit per-run:
* `firm_websites_pilot_<run-ts>.csv` — all 30 rows
* `firm_websites_unmatched_<run-ts>.csv` — anything flagged
* `firm_websites_platform_breakdown_<run-ts>.csv` — counts by
  detected platform, useful for understanding where the parsing
  effort should focus next

Total request budget: ~30 firms × ~3 pages each (home + nav crawl +
about) = ~90 requests. At 0.5 rps that's ~3 minutes.

## 10. Out of scope (for now) (suggested)

* Per-attorney profile pages on firm sites -- not in scope of project.
* OCR of image-rendered text.
* Auto-generated firm descriptions for the database
  (`description_blurb` is the raw input; the final description is a
  later decision — LLM, template, or human-edited).


## 11. Other things to consider

* CloudFlare-backed sites
* JavaScript-utilizing sites

---

## 12. Recon findings & extraction rules (AGENT-VALIDATED 2026-06-02)

Probed 41 live firm sites (16 curated incl. all the §-examples + 25
random from the DB) plus 6 team pages. The suggestions above largely
hold; this section pins them into concrete, evidence-backed rules.

### 12.1 Reachability / anti-bot (random sample, n=25)
- **~88% reachable.** The rest are dead/parked domains -> `url_unreachable`.
- **Real Cloudflare hard-blocks are rare** (0/25 random; only big national
  firms like `bipc.com` did it). Detect a REAL challenge by **HTTP
  403/429/503 OR `<title>` == "Just a moment..." OR <~400 chars of body
  text** — NOT by substring-scanning for `cloudflare`/`cf-*` (those
  scripts ride on many normal pages; my first pass false-flagged
  Goetz/TEPLG/Kutak as challenged when they loaded fine). Do not
  fight challenges; flag and move on (same posture as Avvo/FindLaw).
- **JS-rendering** is rare on small/mid firms (all had ample
  server-rendered text) but common on **big-firm SPAs** (Holland & Hart
  `/people` returned 0 parseable people). Treat thin text, or 0 people
  on a clearly-multi-attorney firm, as a headless-render candidate
  (lazy / opt-in, not by default).

### 12.2 Legal-relevance gate
- Rule: a legal **JSON-LD @type** (`LegalService`/`Attorney`/`Lawyer`,
  lower-cased + whitespace-stripped — types can have a leading space)
  **OR >= 2 legal text tokens** in title + first ~3k chars -> "legal".
- Coverage: legal JSON-LD on ~27% of sites; text-token gate caught
  ~91%. Use both (JSON-LD is bonus, text is the workhorse).
- ~2/22 minimalist real-firm homepages scored only 1 token ->
  **never auto-drop**: `not_a_law_firm` is a REVIEW flag, and check the
  about/team page before flagging. Correctly rejects `swissbiologic.com`.

### 12.3 Headcount — a CASCADE; no single method is universal
Each method validated against a real page that ONLY it solved:
1. **Stated count** (highest confidence): regex `(\d[\d,]*)\s*\+?\s*
   (attorneys?|lawyers?)` over home+about+team. Handle commas
   (`1,000` not `000`); DROP year-like values (1900–2099) unless
   corroborated; store the raw phrase. — Caritas team "20 Attorneys",
   Kutak 600, Proskauer 800, Goetz-home 40.
2. **Profile-link count**: distinct internal links matching
   `/(attorneys?|lawyers?|people|team|bio)/{slug}` on the team page. —
   Goetz `/attorneys` = **42** (≈ real 40+).
3. **Heading-role classification**: count `h2/h3/h4` person-cards,
   classify attorney vs staff by role keywords (§4 table). — TEPLG =
   **3 attorneys / 4 staff** (exact).
4. **Solo signal -> count = 1**: singular "Our Attorney" nav, exactly
   one bio, or first-person-singular copy. — KD Law, Draitser, Entrekin.
5. **All-fail -> `attorney_count_unknown` + review**: thin/JS page, or 0
   people on a clearly-multi-attorney firm. — Holland & Hart (~500 SPA).

Take the highest-confidence available; when methods disagree (Davis
Miles: 0 links but 6 role-headings) keep the max-plausible, store all,
lower confidence, flag. Footer addresses-with-phones > marketing big
numbers. Always store the snapshot date; never overwrite a newer figure
with an older fetch.

### 12.4 Page discovery (validated)
The nav-keyword scan (`about|firm|team|people|attorney|lawyer|our story|
who we are`) found a usable about/team link on essentially every
reachable site -> it's the reliable path. Ordered known-path guesses
(`/about`, `/our-team`, ...) are a fast first try; home page is the
final fallback (solos). Cache `domain -> discovered pages`.

### 12.5 New gotchas (beyond §1–11)
- Comma headcounts (`1,000`) and year-misfires (`© 2019 ... Attorneys`).
- JSON-LD `@type` can have a leading space.
- ~12% of stored websites are dead domains; **154 are emails**
  (`x@yahoo.com` in `FirmURL`) -> invalid, flag, don't crawl. (Candidate
  upstream fix: reject `@`-containing values in `normalize_url`.)
- Platform mix (this sample): WordPress-heavy, then Squarespace / custom
  / Wix / Scorpion / Webflow. No single template -> the general cascade
  beats per-template parsers; use platform only as a hint.

---

## 13. Build + pilot results (AGENT-VALIDATED 2026-06-03)

Built the enrichment as `enrichment/website_extract.py` (pure cascade),
`website_enrichment` table (keyed by normalized website),
`scrapers/website.py` (`FirmWebsiteScraper`), and the producer/worker pipeline
`pipelines/enrich_websites.py` (set-difference producer → thread-pool workers →
single bulk-upsert committer; `pilot`/`run`/`load`). 27,252 distinct
non-aggregator websites are in scope today (Justia + FindLaw + AZ Bar).

### 13.1 Pilot vs. expected (known firms)
Verified the cascade against firms whose answers we know:

| Firm | attorneys | staff | offices | years | status | method |
|---|---|---|---|---|---|---|
| Goetzlaw.com | 40 | 600 | 4 | 25 | verified | stated (big-H2) |
| bipc.com | 475 | – | **– (miss)** | 175 | verified | stated (prose) |
| martinbonnett.com | 6 | – | – | 32 | verified | heading_roles |
| teplg.com | 3 | 4 | 1 | 16 | verified | heading_roles |
| lanceentrekin.com | 1 | – | 1 | 25 | verified | solo |
| swissbiologic.com | – | – | – | – | not_a_law_firm | (gated out) |

Goetz/BIPC/TEPLG/Entrekin all match reality after the fixes below.

### 13.2 Edge cases the pilot surfaced (and fixes)
- **Sparse home → false `not_a_law_firm`.** TEPLG's Wix home carried only 1
  legal token; the team page is clearly a firm. FIX: run the relevance gate
  over **all** fetched pages (OR), not the home alone.
- **Solo over-counted.** Discovery followed Entrekin's practice-area pages
  (`/phoenix-car-accident-attorney`) and `_heading_roles` counted each page
  *title* as a person → 3. FIX: `_heading_roles` only counts headings that look
  like a **person name** (2–4 capitalized tokens, no role/practice/marketing
  words), so page titles don't inflate the count (Entrekin → 1 solo).
- **Load failures.** 15s timeout + 1 retry + https→http fallback; a dead/parked
  domain is recorded `unreachable` and the worker moves on (never hangs).
- **Multi-subpage rosters** (Martin & Bonnett): headcount UNIONS profile-links
  and SUMS heading-roles across all discovered attorney sub-pages.

### 13.3 Known remaining gaps (tuning, not blockers)
- **BIPC offices missed.** "16 offices" lives on its non-obvious about page
  (`/about-buchanan`) / an offices page we didn't reach; office_count came back
  null. Office count is secondary to headcount; revisit discovery for
  non-obvious about URLs if office coverage matters.
- **`heading_roles` on big-firm SPAs** can still under/over-count; those are
  `needs_render` candidates (deferred headless pass).
- Discovery occasionally follows a vanity/news link whose text contains
  "attorneys" — harmless (extra page fetch), tighten `_SKIP_PATH` if noisy.

### 13.4 Verdict
The general cascade + producer/worker + bulk-upsert architecture is validated
on real firms. Ready for a full background `run` over the 27k sites (its own
checkpoint via `enriched_at`); the LLM description layer is the remaining
increment (needs `ANTHROPIC_API_KEY`).
