# AZ Bar Website Reference

Technical reference for the Arizona State Bar's online member directory. Pair this with `claude_code_context.md` when implementing the AZ Bar scraper.

## Mental model

The Arizona Bar's member directory at `www.azbar.org` is a thin JavaScript front-end over a REST-like JSON API hosted at `api-proxy.azbar.org`. Almost no useful data lives in the HTML — when the browser renders an attorney page, JavaScript fetches JSON from the API and injects it into the DOM client-side.

**Implication for scraping:** Do not focus on parsing the HTML. Call the JSON API directly. The whole HTML/CSS layer is irrelevant.

The API has the shape of a Microsoft .NET WebAPI service (the `X-Powered-By: ARR/3.0` response header points to Microsoft Application Request Routing). Query parameters tend to use PascalCase. Responses are wrapped in a consistent envelope (see below).

## Base URL

```
https://api-proxy.azbar.org
```

All endpoints are paths under this host. The `api-proxy` subdomain is what the suffix suggests — a proxy in front of the actual member-data backend. From the scraper's perspective it behaves like a normal HTTP API.

## Required request headers

Every API call must include these headers. Missing the `Password` header will likely return 401/403.

```
Accept: application/json, text/javascript, */*; q=0.01
Content-Type: application/json; charset=UTF-8
Password: <static UUID — see below>
Referer: https://www.azbar.org/
User-Agent: <polite project-identifying string>
```

### The Password header

The AZ Bar API requires a static `Password` header. It's a UUID embedded in the front-end JavaScript that the same proxy uses to authenticate all calls coming from the official website. It is **not** a per-user credential — it's the same value for everyone, baked into the public JS bundle.

The value as observed in DevTools at the time this doc was written:

```
Password: 12B631CC-5922-4EF8-8978-23CF2F32EA8D
```

**Important:** This may rotate at any time. Before scraping, verify the current value by:

1. Opening DevTools on `https://www.azbar.org/` in a browser.
2. Triggering a search (any query, or empty search).
3. In the Network tab, finding any `api-proxy.azbar.org` request.
4. Copying the `Password` header value from the Request Headers section.

Store the value in `.env` as `AZBAR_API_PASSWORD=...`. Do not commit it to git. Add `.env` to `.gitignore` (already covered by the base project template).

## Endpoints

### Search/List (paginated attorney listing)

```
POST https://api-proxy.azbar.org/MemberSearch/Search/?PageSize={N}&Page={P}&RequestorEntityNumber=undefined&Shuffle=true&Seed=null
```

Yes — it's a **POST** request despite being a search. The body can be an empty JSON object `{}` or omitted. The query parameters are what matter.

Parameters:
- `PageSize` — number of results per page. Default observed: 25. Larger values likely accepted but not yet tested.
- `Page` — 1-indexed page number.
- `RequestorEntityNumber` — pass the literal string `undefined`. This is what the front-end sends; the API tolerates it.
- `Shuffle` — boolean. `true` randomizes order; `false` may give deterministic order (untested — try this first if you want reproducibility).
- `Seed` — shuffle seed. Pass `null` for random.

**Response envelope** (all endpoints use this wrapper):

```json
{
  "IsSuccess": true,
  "IsWarning": false,
  "EntityNumber": null,
  "Message": "",
  "IsResult": true,
  "Error": "",
  "Result": {
    "Seed": 3,
    "Page": 1,
    "PageSize": 25,
    "TotalCount": 25944,
    "Results": [ /* array of attorney records */ ]
  }
}
```

Key observations:
- `TotalCount = 25944` at time of writing → roughly **1,038 pages** at PageSize=25 for the full directory.
- `Results` is the array of attorney summary records (shape below).
- Always check `IsSuccess` and `Error` before processing `Result`.

### Search/Detail (single attorney)

```
GET https://api-proxy.azbar.org/MemberSearch/Search?EntityNumber={ID}&RequestorEntityNumber=undefined&{}
```

Same path as list, but with `EntityNumber` instead of pagination params. **GET, not POST.** Returns the full record for one attorney (presumably including practice areas, certifications, biography, additional phone numbers).

**Response shape: NOT YET CONFIRMED.** This endpoint must be sampled during reconnaissance before parser code is written. Save the response to `tests/fixtures/az_bar/sample_detail.json` and update this doc with the field list.

### Specializations (reference data)

```
GET https://api-proxy.azbar.org/MemberSearch/Specializations?{}
```

Returns the 10 formal AZ Bar certified specializations. These are *not* the same as self-reported practice areas — these are credentialed certifications earned through continuing education and peer review. Only a fraction of attorneys hold any of these.

Confirmed response (this is the entire list — there are only 10):

```json
[
  {"CertifiedSpecializationCode": "AL", "CertifiedSpecializationDescription": "Administrative Law"},
  {"CertifiedSpecializationCode": "BY", "CertifiedSpecializationDescription": "Bankruptcy Law"},
  {"CertifiedSpecializationCode": "CD", "CertifiedSpecializationDescription": "Construction Defect Law"},
  {"CertifiedSpecializationCode": "CR", "CertifiedSpecializationDescription": "Criminal Law"},
  {"CertifiedSpecializationCode": "ET", "CertifiedSpecializationDescription": "Estate & Trust Law"},
  {"CertifiedSpecializationCode": "FL", "CertifiedSpecializationDescription": "Family Law"},
  {"CertifiedSpecializationCode": "PI", "CertifiedSpecializationDescription": "Injury & Wrongful Death Litigation"},
  {"CertifiedSpecializationCode": "RE", "CertifiedSpecializationDescription": "Real Estate Law"},
  {"CertifiedSpecializationCode": "TX", "CertifiedSpecializationDescription": "Tax Law"},
  {"CertifiedSpecializationCode": "WC", "CertifiedSpecializationDescription": "Workers' Compensation Law"}
]
```

Use these to seed the `certified_specializations` table. Note that `PI` ("Injury & Wrongful Death Litigation") is the credential most relevant to this project's personal-injury focus.

Note also: this response is **not** wrapped in the standard envelope — it's a bare JSON array. The wrapper behavior may not be consistent across all endpoints. Test each endpoint's wrapper assumption before relying on it.

### ABS / Alternative Business Structures

```
GET https://api-proxy.azbar.org/MemberSearch/ABS?IncludeInactive=true&{}
```

Returns the list of AZ-registered ABS firms (Arizona allows non-lawyer ownership of law firms — a state-specific rule effective 2021). The `IncludeInactive=true` parameter includes ABS firms that have ceased operating.

**Response shape: NOT YET CONFIRMED.** Sample during reconnaissance. Save to `tests/fixtures/az_bar/sample_abs.json`. Use the result to mark `Firm.is_abs = true` on the canonical firm table during the loader phase.

### Reference dropdowns

Six small endpoints that populate UI filter dropdowns. Fetch each once per scrape run and save as reference fixtures:

```
GET https://api-proxy.azbar.org/MemberSearch/States?{}
GET https://api-proxy.azbar.org/MemberSearch/Counties?{}
GET https://api-proxy.azbar.org/MemberSearch/Jurisdictions?{}
GET https://api-proxy.azbar.org/MemberSearch/Languages?{}
GET https://api-proxy.azbar.org/MemberSearch/LawSchools?{}
GET https://api-proxy.azbar.org/MemberSearch/Sections?{}
```

Sizes (compressed, from DevTools): States 3.8 KB, Counties 724 B, Jurisdictions 15.3 KB, Languages 4.3 KB, LawSchools 21.6 KB, Sections 3.7 KB. Small reference data — fetch sequentially, no concurrency needed.

`Sections` refers to **AZ Bar sections** (Family Law Section, Litigation Section, etc.) — voluntary professional groupings within the bar association. Different concept from "Areas of Law." May be useful as a secondary signal of an attorney's focus.

### Endpoints to ignore

Visible in DevTools but not data-bearing for our purposes:

- `logInButton`, `IsLoggedIn` — UI state. Ignore.
- `widget-settings/?id=...` — third-party widget. Ignore.
- `collect?v=2&tid=G-QVJRJLLE41&...` — Google Analytics tracking. Ignore.
- `log?format=json&hasfast=true&authuser=0` — Google's own telemetry. Ignore.

## List response — attorney summary fields

A complete sample record from a `Search/?Page=1` response:

```json
{
  "EntityNumber": 35371,
  "BarNumber": "016435",
  "FirstName": "Erika",
  "MiddleName": "Lee",
  "LastName": "Cossitt Volpiano",
  "Company": "Erika L. Cossitt Volpiano, P.C.",
  "Address": {
    "Address1": "1001 N 6TH AVE",
    "Address2": null,
    "City": "TUCSON",
    "State": "AZ",
    "Zip": "85705-8025",
    "County": "Pima"
  },
  "Email": "",
  "PhoneNumbers": [],
  "ProfilePicUrl": "",
  "MemberStatus": "Active",
  "IsProBonoCounsel": false,
  "BillCode": "5",
  "PrimaryPhone": "",
  "MemberType": "Member"
}
```

Field notes:

- `EntityNumber` (int) — internal AZ Bar ID. **Use this for the detail endpoint, not BarNumber.** Treat as the natural unique key for an attorney within AZ Bar's system.
- `BarNumber` (string) — the public bar number. Has leading zeros (`"016435"`, `"004708"`). **Always store as string.** Combined with state (`"AZ"`), this is the cross-source natural key for `Person`.
- `Company` (string, possibly empty) — the firm name as the attorney has registered it with the bar. Subject to all the usual firm-name variation problems (different abbreviations, comma placement, etc.). The loader is responsible for normalizing across attorneys.
- `Address` (object) — see "Data quirks" below.
- `Email` (string, often empty) — many attorneys leave this blank in the public directory.
- `PhoneNumbers` (array) — **empty in all observed list responses.** Real phone data likely lives in the detail endpoint.
- `PrimaryPhone` (string, also typically empty in list) — same caveat.
- `ProfilePicUrl` (string, often empty) — hosted at `membertools.azbar.org/Dashboard/uploads/{EntityNumber}-Profile.{jpg|JPG}`. Note inconsistent extension casing.
- `MemberStatus` — observed values: `"Active"`. Other possible values (Inactive, Retired, Suspended, etc.) likely exist but are unobserved.
- `IsProBonoCounsel` (bool) — flags pro bono attorneys.
- `BillCode` — billing code. Probably internal/administrative. Ignore unless it turns out to encode useful information.
- `MemberType` — observed values: `"Member"`. Other values likely exist (Affiliate, Honorary, etc.) — surface them in the loader.

## Detail response — UNCONFIRMED

The detail endpoint (`Search?EntityNumber=...`) has not yet been sampled. Expected to contain at minimum:

- All summary fields above (likely a superset).
- Practice areas / "Areas of Law" — the user-facing self-reported list.
- Certified specializations held by the attorney (mapped to the `CertifiedSpecializationCode` values above).
- Phone numbers populated.
- Possibly: biography text, bar admission date, law school, languages spoken, sections of the bar joined.

**First reconnaissance step:** open a profile in the browser with DevTools open, capture the JSON response, save to `tests/fixtures/az_bar/sample_detail.json`. Update this doc with the confirmed shape before writing the parser.

## Pagination behavior

What's confirmed:

- `Page=1` returns the first 25 records (at default `PageSize`).
- `TotalCount` in the response gives the total result set size.
- Sequential pagination works.

What needs to be confirmed during reconnaissance:

- Behavior when `Page` exceeds the last valid page — does it return an empty `Results` array (clean stop signal), a 404, an error in the envelope, or something else?
- Whether `Shuffle=false` produces stable ordering across requests (matters for resumable scraping).
- Maximum allowed `PageSize` — larger pages mean fewer HTTP roundtrips. Try 100 or 200; back off if rejected.

Recommended loop logic:

```python
page = 1
while True:
    response = fetch_list_page(page)
    if not response["IsSuccess"]:
        raise APIError(response["Error"])
    results = response["Result"]["Results"]
    if not results:
        break  # past the last page
    yield from results
    page += 1
```

Belt-and-suspenders: also check `len(results) < PageSize` as a sign of the last page, in case the API returns one short page rather than an empty page at the end.

## Data quirks (handle in the normalizer)

Observed in the list response sample; likely all apply to detail responses too.

**Inconsistent string casing.** Cities appear as `TUCSON`, `Phoenix`, `Anthem` — no normalization on AZ Bar's side. Normalizer should title-case city names while preserving the raw value in `FirmSourceRecord`.

**Whitespace in numeric/structured fields.** At least one Zip code observed with trailing spaces: `"85003     "`. Strip whitespace on all string fields before storing the normalized version.

**Mixed null and empty string for "no value."** `Address2` is sometimes `null`, sometimes `""`. Other optional fields (Email, PhoneNumbers, ProfilePicUrl, PrimaryPhone) frequently empty string rather than null. Treat both as "no value" semantically; normalize to consistent representation (probably `None` in Python, NULL in DB).

**Leading zeros in BarNumber.** `"016435"`, `"004708"`. Storing as int would lose these. **String always.**

**Address case mixing within one record.** A single record can have `City: "Phoenix"` and `State: "AZ"` (mixed case in adjacent fields). Don't assume internal consistency within an address.

**Trailing whitespace in firm names.** Observed: `"Company": "Fennemore Craig PC  "`. Trim.

**Inconsistent file extension casing in URLs.** ProfilePicUrl can end in `.jpg`, `.JPG`, or other variants. URL normalization should lowercase the path.

**Duplicate firms across attorneys.** Multiple attorneys at the same firm share identical firm fields (e.g., John Ager and William Sandweg both at `"Sandweg & Ager PC"`, same address). This is expected and is what triggers the firm-grain aggregation at the loader layer.

## Concurrency and rate limiting

Project target: full scrape in 30 minutes or less (~27,050 requests). 

What's known about server-side limits:

- No documented rate limit.
- Observed list endpoint response time: ~2 seconds. Detail endpoint response time: unknown but probably comparable.
- The `api-proxy` layer suggests a CDN or load-balancer in front of the real backend — these typically have generous burst tolerance but may throttle sustained high request rates.

Recommended approach:

1. Start at 10 concurrent workers with a 15 req/sec global cap.
2. Monitor for 429 (Too Many Requests) and 5xx responses.
3. On throttling: exponential backoff AND reduce active concurrency (don't just delay individual workers — back off the whole pool).
4. Tune empirically; document the working values in `docs/data_sources.md`.

## Required reconnaissance before writing scraper code

Do these before writing any production scraper logic. Save outputs as fixtures so the parser can be built against real data.

1. **Re-verify the `Password` header value.** Check it's still `12B631CC-5922-4EF8-8978-23CF2F32EA8D` (or fetch the current value). Store in `.env`.
2. **Sample the detail endpoint.** Click into one profile in the browser, capture the JSON, save to `tests/fixtures/az_bar/sample_detail.json`. Use a personal injury attorney if possible so the practice-area data is rich. Update this document with the confirmed field shape.
3. **Sample the ABS endpoint.** Fetch `ABS?IncludeInactive=true&{}`, save to `tests/fixtures/az_bar/sample_abs.json`. Confirm shape, decide whether `Firm.is_abs` boolean is sufficient or whether more fields warrant their own table (e.g., ABS registration date, license number, ownership structure).
4. **Test pagination edges.** Fetch page 1, page 1038 (or near the last page), and page 1100 (well past the end). Document the behavior at the edge.
5. **Test the larger `PageSize` values.** Try 100 and 200. If the API accepts them, the full scrape needs many fewer HTTP roundtrips.
6. **Try filtering by `CertifiedSpecializationCode`.** Add `&CertifiedSpecializationCode=PI` to a list call; see if it filters. If yes, this is a fast path to the priority subset of attorneys (~few hundred PI-certified) without scraping the full 26K.
7. **Sample one reference dropdown** (e.g., `Sections?{}`) and check whether its response uses the standard envelope or a bare array (as `Specializations` does).

## Suggested scrape phase ordering

1. **Reference data.** Fetch `Specializations`, `States`, `Counties`, `Jurisdictions`, `Languages`, `LawSchools`, `Sections`, `ABS`. Save as fixtures. Total: ~10 requests. Fast.
2. **List sweep.** Iterate `Page=1` through the last page. Save each page's full response to disk. Total: ~1,040 requests. Extract every `EntityNumber` into a work queue.
3. **Detail sweep.** Concurrent workers consume the EntityNumber queue, fetching `Search?EntityNumber={id}` for each. Save each detail response. Total: ~26,000 requests. This is the bulk of the scrape time.
4. **Verify.** Spot-check 20 random saved detail files against the live site (open in browser) to confirm the data matches.

Step 4 is the validation step the milestone plan calls for at the end of Milestone 4 ("Run small test scrape (~20 records) and inspect DB rows" — same idea, expanded).

## What lives where, on disk

```
data/raw/az_bar/{YYYY-MM-DD}/
├── reference/
│   ├── specializations.json.gz
│   ├── abs.json.gz
│   ├── states.json.gz
│   ├── counties.json.gz
│   └── ...
├── list/
│   ├── page_0001.json.gz
│   ├── page_0002.json.gz
│   └── ...
└── detail/
    ├── 35371.json.gz
    ├── 35531.json.gz
    └── ...
```

The date in the path is the date the scrape *started*. A single scrape doesn't span dates — if a scrape happens to cross midnight, files stay in the original date's folder. This keeps a scrape run self-contained.

## Open questions 

- **ABS schema shape** — same. Boolean column or a separate table depends on the ABS endpoint's response richness.
- **Sections as a tracked signal** — should AZ Bar voluntary section memberships be modeled as a separate join table (`person_sections`), or stored opaquely in `additional_data`? Ask the user once the Sections endpoint shape is confirmed.
- **MemberStatus filtering** — should the scraper include inactive/retired/suspended attorneys, or limit to `Active`? Default to including everyone (filter at query time, not at scrape time) but confirm.
