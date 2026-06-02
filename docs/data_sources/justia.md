# Justia Lawyer Directory — data source notes

Host: `https://www.justia.com` (the directory path is `/lawyers/...`).
NOTE: `https://lawyers.justia.com/lawyers/...` **301-redirects every
request to `www.justia.com/lawyers/...`** — the scraper targets `www`
directly to skip the hop. `www.justia.com/robots.txt` is fully open
(`User-agent: * / Disallow:`).

Status: **RECON COMPLETE 2026-06-02** — viable with a browser-shaped
header set. Scraper/parser/pipeline built same day.

## Access posture

- **robots.txt** (`lawyers.justia.com/robots.txt`): `User-agent: *`
  `Allow: /`, only `/claim/`, `/image_captcha/*`, `/web*` disallowed
  (and `PiplBot` blocked from `/search*`). The directory pages we use
  (`/lawyers/...`) are allowed. CONFIRMED 2026-06-02.
- **Cloudflare-fronted, but passes with realistic headers.** A request
  with a *minimal* header set gets a `403 "Just a moment..."` Cloudflare
  challenge. The SAME request with a full browser header set
  (`sec-ch-ua`, `sec-ch-ua-platform`, `Sec-Fetch-*`,
  `Upgrade-Insecure-Requests`) returns `200`. So the gate is header
  shape, NOT a JS/JA3 challenge — no TLS-impersonation or headless
  browser needed. CONFIRMED 2026-06-02 (minimal→403, full→200, 200,
  200 across several paths). The scraper still treats a "Just a moment"
  body as a fatal `JustiaCloudflareChallenge` (defensive — if Cloudflare
  tightens, stop, do not fight it).

## URL structure (all 200 with full headers)

- State directory: `/lawyers/{state}` (e.g. `/lawyers/california`).
  Lists lawyers state-wide, ~52 cards/page.
- City: `/lawyers/{state}/{city}` (e.g. `/lawyers/california/los-angeles`).
- Practice area + state: `/lawyers/{practice-area}/{state}`.
- Practice area + state + city: `/lawyers/{practice-area}/{state}/{city}`.
- Pagination: `?page=N`, with `<a rel="next" href="/lawyers/{state}?page=2">`.
  **Follow `rel="next"`** — do not blind-increment.

### Pagination cap (like Martindale)

State pagination is capped. `?page=50/150/300` on a big state all
**redirect to `www.justia.com/lawyers/{state}`** (host changes, page
param dropped) and return a no-`rel=next` page. Following `rel="next"`
stops cleanly at the cap. Consequence: **mega-states are only partially
covered at the state grain.** Subdivision by city and/or practice area
(URL builders provided) is the future lever to reach deeper — same
situation as Martindale's 167-page cap. Documented; not yet implemented.

State slugs: lowercase hyphenated full name (`california`, `new-york`).
DC slug not yet confirmed; the pipeline logs and skips any state that
404s on its index.

## Card structure (the parser's contract)

Each listing is `div.jld-card` with a modifier class:
`-organic` (free listings) or `-premium`/`-gold` (paid). ~52/page,
of which ~40 are `-organic`.

**Justia is ATTORNEY-level** (one card = one lawyer), like AZ Bar and
Martindale SRP cards. Fields on an `-organic` card:

- `data-vars-profile` (card attr) → stable numeric attorney id.
- `a[href*="/lawyer/"]` → profile URL `/lawyer/{slug}-{id}`
  (a `/contact` variant also appears; take the bare profile URL).
- `strong.name > a` → lawyer name.
- `.address` → office address as `<br>`-separated lines: street
  line(s) then `City,  ST  ZIP` (whitespace is tab-heavy). Split on
  `<br>`; the last line matches `City, ST ZIP`.
- `.phone` / `a[href^="tel:"]` → phone.
- `.outline` → practice areas, human text joined with `,` and ` and `
  (e.g. `"Employment, Personal Injury and Workers' Comp"`).
- website: an `<a>` whose class/text mentions "website" → the firm's
  external site URL.

**No firm name appears on the listing card.** `-premium` cards are
marketing-heavy and often omit the address entirely.

## Firm grain (see docs/assumptions.md 2026-06-02 entry)

Because listings carry a lawyer + address but no firm name, Justia
`FirmSourceRecord`s are aggregated by **normalized office street**:
lawyers sharing a street → one firm-shaped record (`name_raw=None`,
those lawyers as `contacts`, practice areas unioned, `attorney_count`
= contact count). A lawyer with no parseable street stays a solo
record keyed on the attorney id. Cross-source resolution matches Justia
records to named firms from other sources via phone / website / address
(not name).

## Rate posture

Cloudflare-fronted → treat as sensitive. Scraper defaults: 0.5 RPS
sustained, `0.33→0.5` ramp, burst 1, 1 worker, browser UA + full
header set. First `403`/"Just a moment"/interstitial → stop and revisit.
