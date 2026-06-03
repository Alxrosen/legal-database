# Additional Firm Websites to Scrape — Bar Associations

One entry per US jurisdiction (50 states + DC). Format:
`State; URL or "not readily available"; terse note on scrapability,
parameters, firm-field availability, anti-bot, result count, quirks.`

## The list

- Alabama; https://members.alabar.org/Member_Portal/Member_Portal/Member-Search.aspx; VERY slow, be polite.
- Alaska; https://alaskabar.org/for-lawyers/member-directories/; requires SOME parameter put in to search bar, so can put United States in country parameter. Returns ~5185 matches. "Organization" specifies the law firm.
- Arizona; (completed)
- Arkansas; not readily available.
- California; skipped.
- Colorado; https://www.licensedlawyer.org/Lawyers/DoSearch; very difficult to scrape because requires many parameters and cloudflare is used.
- Connecticut; https://www.jud.ct.gov/attorneyfirminquiry/attorneyfirminquiry.aspx; Judicial Branch lookup (CT Bar Assoc. is voluntary); ASP.NET form requires last name; firm field present. CBA find-a-lawyer is voluntary opt-in only.
- Delaware; https://rp470541.doelegal.com/vwPublicSearch/Show-VwPublicSearch-Table.aspx; Supreme Court doelegal public search, ~3,200 attorneys; DSBA legal directory is members-only/paywall; firm field included.
- District of Columbia; https://join.dcbar.org/eWeb/DynamicPage.aspx?Site=dcbar&webcode=findmember; mandatory unified bar (~110k); search page protected by "verify you are not a robot" captcha, hard to scrape.
- Florida; https://www.floridabar.org/directories/find-mbr/; mandatory bar (~110k members); rich form (name/firm/city/practice); firm field exposed; publicly scrapable, no captcha visible.
- Georgia; https://www.gabar.org/member-directory; mandatory bar (~55k); JS-rendered Sitefinity search returns shell on fetch; firm field exposed via reliaguide find-a-lawyer; existing Apify scrapers exist.
- Hawaii; https://hsba.org/member-directory; mandatory bar (~6k); JS-heavy SailAMX SPA, returns empty shell on fetch; firm field present in UI; needs headless browser.
- Idaho; https://isb.idaho.gov/licensing-mcle/attorney-roster-search/ (data: https://apps.isb.idaho.gov/licensing/attorney_roster.cfm); public, requires last-name prefix; firm/employer field exposed; ~7k members; straightforward A–Z sweep.
- Illinois; https://iardc.org/Lawyer/Search; ARDC mandatory roll (~90k); public form requires last name (or city/county/etc.); firm/business address exposed; ISBA voluntary separate.
- Indiana; https://courtapps.in.gov/rollofattorneys/search; mandatory Roll of Attorneys via Supreme Court; React SPA returns shell on fetch; firm field present; needs headless or API discovery.
- Iowa; https://www.iacourtcommissions.org/ords/f?p=106:10; Oracle APEX public search on Supreme Court site; filter by name/county/status with no required field; firm location returned; ISBA find-a-lawyer is voluntary opt-in only.
- Kansas; https://directory-kard.kscourts.gov/; Supreme Court mandatory directory (KBA is voluntary); any-part-of-name search; firm field present; small JS app but lightweight, scrapable.
- Kentucky; https://kybar.org/cv5/cgi-bin/utilities.dll/openpage?WRP=LawyerLocator.htm; mandatory unified bar (~19.5k); form with optional fields, no required field; returns up to 105k records shown counter; firm/city/county exposed.
- Louisiana; https://www.lsba.org/MD321654/MembershipDirectory.aspx; CAPTCHA-gated entry to LSBA directory; mandatory bar; firm/organization shown in profiles; captcha makes scraping painful.
- Maine; https://apps.web.maine.gov/cgi-bin/online/maine_bar/attorney_directory.pl; Board of Overseers (not bar); WebFetch returned empty JS shell, but detail URLs are predictable by bar_num; firm field exposed.
- Maryland; https://www.mdcourts.gov/attysearch; Judiciary AIS-backed; partial last name searches; ~40k attorneys; firm field present; voluntary MSBA.
- Massachusetts; https://www.massbbo.org/s/attorney-lookup; Salesforce site, JS-heavy CSS-error shell on fetch; BBO (judicial); needs name/BBO#; firm shown.
- Michigan; https://sbm.reliaguide.com/ (plus classic at directory.michbar.org); mandatory bar; ReliaGuide is full JS SPA; classic directory simpler; firm exposed.
- Minnesota; https://mars.courts.state.mn.us/; judicial MARS system; offers bulk CSV/XML download of all lawyers (name/city/zip only, no firm); detailed search returns firm but requires name.
- Mississippi; https://courts.ms.gov/bar/barroll/barroll.php; judicial bar roll, simple form by name/city/zip; WebFetch empty so likely JS or POST; firm visible in profiles.
- Missouri; https://mobar.org/public/LawyerDirectory.aspx; mandatory bar; capped at 250 results per query so requires many partial queries; firm/city only minimally exposed.
- Montana; https://www.montanabar.org/cv5/cgi-bin/utilities.dll/openpage?WRP=membersearch.htm; mandatory bar; simple GET form with name + member type; firm field in profile.
- Nebraska; https://attorneys.nejudicial.gov/member-search (judicial) or https://www.nebar.com/search/search.asp (bar); NESC is React JS-only shell; nebar has older ASP form; firm shown.
- Nevada; https://nvbar.org/for-the-public/find-a-lawyer/; WebFetch returned empty (JS-rendered); mandatory bar; firm field exposed in profiles.
- New Hampshire; https://www.nhbar.org/find-a-lawyer/; mandatory bar but no public scrapable directory; LRS only, "must call to verify"; firm field not exposed publicly.
- New Jersey; https://portalattysearch-cloud.njcourts.gov/prweb/PRServletPublicAuth/app/Attorney/!STANDARD?AppName=AttorneySearch; NJ Judiciary Pega portal behind Incapsula WAF; requires exact last name + first initial; hard to scrape.
- New Mexico; https://www.sbnm.org/For-Public/I-Need-a-Lawyer/Online-Bar-Directory; mandatory bar, public search by name/bar #; profile shows firm, practice areas, admission date.
- New York; https://iapps.courts.state.ny.us/attorneyservices/search; OCA (not voluntary NYSBA) is real source; JS shell; firm name shown; ~370k attorneys; requires name chars to search.
- North Carolina; https://portal.ncbar.gov/verification/search.aspx; public, server-rendered, returns max 250 per query; needs first 2 chars of last name or city+status; firm field present.
- North Dakota; https://www.ndcourts.gov/lawyers; Judicial branch (not bar) hosts clean public list searchable by initial/city/state; ~2,900 members; firm/city shown.
- Ohio; https://www.supremecourt.ohio.gov/attorneysearch/; Supreme Court runs it; JS-rendered shell; accepts any part of name/reg#; firm/employer field exposed.
- Oklahoma; https://ams.okbar.org/eweb/startpage.aspx?site=FALWEB; full member dir requires login; public "Find A Lawyer" is opt-in subset only; firm shown; mandatory bar.
- Oregon; https://www.osbar.org/members/membersearch_start.asp; mandatory bar, simple public search by name/bar#/city; firm field shown on profile; excludes deceased.
- Pennsylvania; https://www.padisciplinaryboard.org/for-the-public/find-attorney; Disciplinary Board (PA has no unified bar); JS/AJAX results; search by name/ID/city/county/status; firm shown; ~75k.
- Rhode Island; https://www.ribar.com/?pg=memberdirectory; mandatory bar dir + court alt at rijrs.courts.ri.gov; RIBA page is JS shell; firm typically shown; ~6k members.
- South Carolina; https://www.sccourts.org/attorneys/; SC Judicial Branch public attorney search (SC Bar's own directory is login-only); search by first/last/bar number; firm field not guaranteed.
- South Dakota; https://findalawyerinsd.com/; referral-only directory (subset of ~3,000 members); UJS no longer hosts a public attorney lookup. Not a complete member directory.
- Tennessee; https://www.tbpr.org/for-the-public/online-attorney-directory; Board of Professional Responsibility (judicial); plain HTML form, at least one field required; exposes name/address/status; firm not a distinct field.
- Texas; https://www.texasbar.com/AM/Template.cfm?Section=Find_A_Lawyer&Template=%2FCustomSource%2FMemberDirectory%2FSearch_Form_Client_Main.cfm&Find=1; ~100k+ attorneys, public, by name/location/practice area; firm exposed on profile; classic ColdFusion, scrapable with polite pacing.
- Utah; https://services.utahbar.org/Member-Directory; master directory of all Utah lawyers; JS-rendered SPA (WebFetch empty); needs headless browser; firm shown on profile.
- Vermont; not readily available; VBA online directory at vtbar.org/online-directory/ is members-only login; voluntary bar; judiciary lists only "good standing" PDFs.
- Virginia; https://member.vsb.org/NGSAttorneySearch/NGSearch.aspx; VSB official search, JS/ASPX shell (postbacks/viewstate); firm field exposed on profile; scrapable with session handling.
- Washington; https://www.mywsba.org/personifyebusiness/LegalDirectory.aspx; WSBA Legal Directory, fully public, ~40k legal pros; Personify ASPX shell, JS-driven; firm exposed; individual LegalProfile.aspx?Usr_ID=... pages scrapable.
- West Virginia; https://mywvbar.org/membership-search-members; public search portal but JS app/possibly Cloudflare (WebFetch empty); per docs results include firm/employer; likely needs headless browser.
- Wisconsin; https://www.wisbar.org/Pages/BasicLawyerSearch.aspx; SharePoint HTML form, ~24k attorneys; Advanced search keyword covers organization/firm; very scrape-friendly, no captcha.
- Wyoming; https://www.wyomingbar.org/for-the-public/hire-a-lawyer/lawyer-search/; mandatory bar, ~3,000 attorneys; form has explicit "Firm / Organization" field plus city/state/WY county; simple WordPress, very scrapable.

## Quick triage (suggested ordering for development effort)

### Tier 1 — easy, fully server-rendered, firm field exposed
*Tackle these next after AZ — high ROI per engineering hour.*
Wyoming, Wisconsin, Idaho, Kansas, North Dakota, Kentucky, Montana,
Oregon, North Carolina, Florida, Illinois, New Mexico, Iowa.

### Tier 2 — server-rendered but with friction
*Doable today, but each needs ~1 quirk handled (session state,
pagination caps, postbacks, captchas you can satisfy once).*
Alabama (slow), Alaska (mandatory param), Connecticut (last-name
required), Delaware (separate vendor), Maryland, Maine, Missouri
(250-cap forces partial sweeps), Pennsylvania, Texas, Tennessee,
South Carolina.

### Tier 3 — JS-rendered, needs headless Chrome
*Push to a later milestone; share one headless harness across them.*
Georgia, Hawaii, Indiana, Massachusetts, Michigan (ReliaGuide variant),
Mississippi, Nebraska (judicial variant), Nevada, New York, Ohio,
Rhode Island, Utah, Virginia, Washington, West Virginia, Minnesota
(detail page).

### Tier 4 — actively hostile or unavailable
*Don't invest scraper time. Backfill from Martindale + firm
websites + the few publicly listed name lists.*
California (skipped), Colorado (Cloudflare + many required params),
DC (captcha-gated), Louisiana (captcha), New Jersey (Incapsula WAF +
exact-match required), Arkansas (no public directory), New Hampshire
(no scrapable directory), Oklahoma (login-walled), South Dakota
(referral-only), Vermont (login-walled).

## Quirks worth knowing across the whole set

* **Mandatory vs voluntary bar matters.** In states like NY, CT, MA,
  PA, MN, MD, NH the state bar association is voluntary — the real
  comprehensive roll is run by the Supreme Court / Judicial Branch /
  Disciplinary Board. Always prefer the mandatory source.
* **"Firm/Organization" exposure is uneven.** Some directories expose
  it as a first-class field (WY, FL, AK, WI advanced search, OR).
  Others only show address (TN, MN bulk export). Plan to backfill
  the missing firm via Martindale.
* **Result caps.** Several systems cap responses at 250 (MO, NC).
  That forces an A–Z + city-prefix sweep to get full coverage.
* **JS-rendered shells** are the dominant blocker. A single shared
  headless-Chrome runner used for all Tier-3 systems would amortize
  the engineering cost.
* **Bulk downloads exist where you wouldn't expect them.** Minnesota's
  MARS exposes a full CSV/XML of every lawyer (name/city/zip, no
  firm). Always check for an explicit "data download" link before
  building a scraper.


## Scope: real bar directories only, no referral-service detours

* **Some state bar associations don't actually run their own directory**
   — they hand the public off to a third-party referral
  service (LRS), Martindale, FindLaw, Avvo, Justia, or a vendor-hosted
  "find-a-lawyer" widget that's really just a subset of paying members.
  Don't scrape those. They're optional opt-in subsets of the bar, not
  the comprehensive roll, and Martindale is already covered by its own
  dedicated scraper (see docs/data_sources/martindale.md) — double-
  sourcing it through a referral redirect adds noise without adding
  coverage. If a state's listed URL turns out to be a redirect to one of
  those services, treat that state the same as "not readily available"
  and move on.

* **A general-purpose multi-bar scraper, not 50 bespoke ones**
  These 50+ jurisdictions are not 50 separate engineering projects.
  Building a one-off scraper per state would be cheap-per-state and
  expensive in aggregate; we'd end up maintaining 50 small fragile
  codebases. Instead, treat the variation across state bars as the
  design driver for one general scraper that takes per-state config
  (URL, form fields, selectors, pagination shape, JS-or-not flag) and
  runs the same pipeline against any of them. This is the same shape the
  firm-website scraper has to handle, so the two efforts can share the
  same generic fetch + parse + extract spine. Don't sink time into
  hand-tuning quirks for any single state beyond what fits in that
  config — if a directory needs more than that, push it down to Tier 4
  ("backfill from elsewhere") and keep moving. The win is breadth, not
  per-state polish.