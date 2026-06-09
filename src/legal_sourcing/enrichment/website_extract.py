"""Firm-website content extraction — the general cascade (pure functions).

Firm sites are 50k bespoke marketing pages on a long tail of platforms, so
per-template parsers don't pay off (docs/data_sources/firm_websites.md §12.5:
"no single template -> the general cascade beats per-template parsers; use
platform only as a hint"). This module is that platform-agnostic cascade:
given already-fetched HTML (NO network here — fetching/crawling lives in the
scraper/pipeline), it extracts the EBITDA-proxy fields we want per firm —
attorney headcount above all, plus offices, years, phones, notable signals, a
description blurb, and a legal-relevance / URL-match check.

Everything here is a PURE function of its HTML input(s): deterministic, no
network, no DB, no clock except an injectable `now_year`. That makes the
documented edge cases (Goetz big-H2, TEPLG team page, BIPC prose, solo
practices, swissbiologic non-law) unit-testable against fixtures.

Headcount is a CASCADE (take the highest-confidence method that fires):
  1. stated count   — regex over copy ("40+ Lawyers", "Our 450 attorneys")
  2. profile links  — distinct /attorneys/{slug} links on a team page
  3. heading roles  — count person-card headings, classify attorney vs staff
  4. solo signal    — singular "Our Attorney" / one bio / first-person -> 1
  5. unknown        — flag for review (thin/JS page)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from selectolax.parser import HTMLParser

from legal_sourcing.normalize.name import looks_like_firm, normalize_firm_name
from legal_sourcing.normalize.phone import normalize_phone
from legal_sourcing.normalize.practice_areas import get_taxonomy
from legal_sourcing.normalize.url import is_aggregator_domain, safe_urljoin, safe_urlparse

# ---------------------------------------------------------------------------
# Vocabulary

# Legal-relevance tokens (the text gate). A hit on >=2 (or a legal JSON-LD
# @type) marks the page "legal".
_LEGAL_TOKENS: tuple[str, ...] = (
    "attorney",
    "lawyer",
    "law firm",
    "law office",
    "legal",
    "counsel",
    "litigation",
    "practice areas",
    "p.c.",
    "llp",
    "pllc",
    "apc",
    "esquire",
    "esq.",
    "bar association",
    "of counsel",
    "plaintiff",
    "defense",
)
_LEGAL_JSONLD_TYPES: frozenset[str] = frozenset(
    {
        "legalservice",
        "legalservices",
        "attorney",
        "lawyer",
        # Generic org/business types — a firm's structured name often lives under
        # LocalBusiness/Organization (e.g. "Treon & Shook, PLLC"). Generic
        # descriptors are still filtered downstream, so this only recovers real names.
        "localbusiness",
        "organization",
        "corporation",
        "professionalservice",
    }
)

# Role keywords for team-page person classification (§4 table).
_ATTORNEY_ROLE = (
    "attorney",
    "lawyer",
    "counselor at law",
    "esq",
    "partner",
    "associate",
    "of counsel",
    "principal",
    "shareholder",
    "counsel",
)
_STAFF_ROLE = (
    "paralegal",
    "legal assistant",
    "assistant",
    "coordinator",
    "secretary",
    "office manager",
    "administrator",
    "receptionist",
    "bookkeeper",
    "clerk",
    "investigator",
)

_PLATFORM_FINGERPRINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # NB: directory_profile is NOT a substring fingerprint — firms publish
    # outbound links to their own Avvo/Justia/Martindale profiles (often in a
    # JSON-LD `sameAs`), so a bare "justia.com" in the HTML is no signal the
    # page IS a directory. It's detected from the page's declared identity
    # (canonical / og:url) in detect_platform() instead.
    ("wix", ("wix.com", "wixstatic.com", "wixsite.com")),
    ("squarespace", ("squarespace.com", "static1.squarespace")),
    ("webflow", ("website-files.com", "webflow.io")),
    ("scorpion", ("scorpion.co", "scorpioncms")),
    ("wordpress", ("wp-content", "wp-json", "wp-includes")),
)

_SOLO_NAV = ("our attorney", "meet our attorney", "meet the attorney", "the attorney")
_FIRST_PERSON = (
    "i am ",
    "i'm ",
    "my practice",
    "my clients",
    "my firm",
    "founding attorney",
    "i have been",
    "i represent",
)
_NOTABLE = (
    ("fortune 500", "Fortune 500"),
    ("fortune 100", "Fortune 100"),
    ("am law", "AmLaw ranked"),
    ("super lawyers", "Super Lawyers"),
    ("best lawyers", "Best Lawyers"),
    ("billions", "recovered billions"),
    ("multimillion", "multimillion-dollar results"),
    ("board certified", "board certified"),
)

# Leading number for "<n> attorneys/staff/offices". Two guards on the first
# digit: (?<![\d.\-]) stops a phone-number tail / longer digit run from being
# read as a count ("...Call 478-621-4980 Lawyers" must NOT yield 4980); and the
# leading digit is [1-9], so a zero-padded section/ordinal marker is rejected
# ("...AI for Legal Practice 02 Attorneys Mentorship..." on burnerlaw.com must
# NOT yield 2). Real counts ("40+ Lawyers", "Our 450 attorneys", "1,100+") still
# match — none are written with a leading zero.
_N = r"(?<![\d.\-])([1-9][\d,]{0,6})"
_STATED_COUNT = re.compile(rf"{_N}\s*\+?\s*(attorneys?|lawyers?)\b", re.I)
_STAFF_COUNT = re.compile(rf"{_N}\s*\+?\s*(staff|employees|professionals|team members)\b", re.I)
_OFFICE_COUNT = re.compile(r"(?<![\d.\-])([1-9]\d{0,2})\s*\+?\s*(offices?|locations?)\b", re.I)
_YEARS = re.compile(r"(\d{1,3})\s*\+?\s*(?:years?|yrs?)\b", re.I)
_FOUNDED = re.compile(r"(?:founded|established|since|serving\D{0,20}since)\D{0,12}(\d{4})", re.I)
_PHONE = re.compile(r"\(?\b\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b")
_CITY_STATE_ZIP = re.compile(r"([A-Za-z][A-Za-z.\s]{1,38}?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?")
# Street-suffix / unit / compound-directional tokens that must NOT be read as
# part of a city when walking back through flat footer text ("...Inverness
# Drive East Englewood, CO" -> "Englewood", not "Drive East Englewood"). Excludes
# "st"/"saint" on purpose: Saint-cities (St Petersburg, St Louis) are common and
# outweigh the rare "<Street> St <City>" leak; a single cardinal "N/S/E/W" is
# caught by the single-letter rule, while the word forms (north/south/east/west)
# are KEPT so directional-prefixed cities (West Palm Beach) survive.
_STREET_STOP: frozenset[str] = frozenset(
    {
        "drive",
        "dr",
        "avenue",
        "ave",
        "boulevard",
        "blvd",
        "road",
        "rd",
        "lane",
        "ln",
        "court",
        "ct",
        "way",
        "place",
        "pl",
        "parkway",
        "pkwy",
        "highway",
        "hwy",
        "circle",
        "cir",
        "terrace",
        "ter",
        "trail",
        "trl",
        "loop",
        "square",
        "sq",
        "plaza",
        "plz",
        "expressway",
        "expy",
        "freeway",
        "fwy",
        "row",
        "floor",
        "fl",
        "suite",
        "ste",
        "unit",
        "apt",
        "building",
        "bldg",
        "room",
        "rm",
        "lobby",
        "tower",
        "level",
        "ne",
        "nw",
        "se",
        "sw",
    }
)
_PROFILE_LINK = re.compile(
    r"/(attorneys?|lawyers?|people|team|bio|profile|our-attorneys?)/[a-z0-9][a-z0-9\-]+/?$", re.I
)


# ---------------------------------------------------------------------------
# Result shapes


@dataclass
class HeadcountResult:
    count: int | None
    is_min: bool = False
    method: str = "unknown"  # stated|profile_links|heading_roles|solo|unknown
    confidence: str = "none"  # high|medium|low|none
    evidence: str | None = None


@dataclass
class SiteExtraction:
    platform: str = "unknown"
    is_law_related: bool = False
    relevance_terms: list[str] = field(default_factory=list)
    # Firm identity from the site (<title>/og:site_name/JSON-LD/H1). Raw +
    # normalized (suffix-stripped) — mirrors FirmSourceRecord.name_*; names the
    # otherwise-nameless Justia-only firms during canonical fusion.
    name_raw: str | None = None
    name_normalized: str | None = None
    attorney_count: int | None = None
    attorney_count_is_min: bool = False
    attorney_count_method: str = "unknown"
    attorney_count_confidence: str = "none"
    attorney_count_raw: str | None = None
    staff_count: int | None = None
    office_count: int | None = None
    office_addresses: list[dict[str, str]] = field(default_factory=list)
    # The firm's PRIMARY office location (where it is set up) — derived from the
    # zip-anchored footer addresses, NOT from jurisdiction/"we serve" copy.
    primary_city: str | None = None
    primary_state: str | None = None
    primary_postal_code: str | None = None
    years_in_operation: int | None = None
    years_is_min: bool = False
    year_founded: int | None = None  # the founding YEAR (e.g. 1947), if stated
    phones: list[str] = field(default_factory=list)
    # Attorneys/people found on the site: [{name_raw, name_normalized, title}].
    contacts: list[dict[str, str | None]] = field(default_factory=list)
    notable_signals: list[str] = field(default_factory=list)
    # Legal specialties the firm advertises (canonical slugs + verbatim). Matched
    # via the practice-area taxonomy, so office LOCATIONS can never leak in here.
    practice_areas: list[str] = field(default_factory=list)
    practice_areas_raw: list[str] = field(default_factory=list)
    practice_areas_unmatched: list[str] = field(default_factory=list)
    scope: str | None = None
    description_blurb: str | None = None
    firm_short_description: str | None = None
    firm_descriptions: list[dict[str, str]] = field(default_factory=list)
    # Defunct-firm signal: 'closed' / 'parked' / None (mirrors FSR).
    deactivation_status: str | None = None
    # Escape hatch for signals without a dedicated column (mirrors FSR).
    additional_data: dict[str, object] = field(default_factory=dict)
    needs_render: bool = False
    url_verification_status: str = (
        "unverified"  # verified|legal_but_mismatched|not_a_law_firm|unreachable
    )


# ---------------------------------------------------------------------------
# Helpers


def _tree(html: str | bytes) -> HTMLParser:
    return HTMLParser(html)


def _visible_text(tree: HTMLParser) -> str:
    for tag in tree.css("script, style, noscript, svg"):
        tag.decompose()
    body = tree.body or tree
    # separator=" " inserts spaces between adjacent elements' text so a big-H2
    # "40+ Lawyers" doesn't fuse with the next block ("...LawyersWe...") and
    # break word boundaries the headcount regex relies on.
    return " ".join((body.text(separator=" ") or "").split())


def _title_and_head(tree: HTMLParser) -> str:
    parts = []
    ti = tree.css_first("title")
    if ti:
        parts.append(ti.text() or "")
    for sel in ('meta[name="description"]', 'meta[property="og:description"]'):
        m = tree.css_first(sel)
        if m:
            parts.append(m.attributes.get("content") or "")
    h1 = tree.css_first("h1")
    if h1:
        parts.append(h1.text() or "")
    return " ".join(parts)


def _jsonld_types(tree: HTMLParser) -> list[str]:
    out: list[str] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text() or ""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for obj in data if isinstance(data, list) else [data]:
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type")
            for tv in t if isinstance(t, list) else [t]:
                if isinstance(tv, str):
                    out.append(tv.strip().lower())
    return out


def _drop_year(n: int) -> bool:
    return 1900 <= n <= 2099


def _to_int(s: str) -> int:
    return int(s.replace(",", ""))


def _host(url: str) -> str:
    """Lowercased netloc of a URL, or '' — never raises (Python 3.14 urlparse
    is strict; scraped hrefs are junk-prone)."""
    p = safe_urlparse(url)
    return (p.netloc if p else "").lower()


# ---------------------------------------------------------------------------
# Platform


def detect_platform(html: str | bytes) -> str:
    tree = _tree(html)
    # directory_profile is decided by the page's OWN declared identity
    # (canonical / og:url host), NOT a substring anywhere in the HTML — a firm's
    # own site linking to its Avvo/Justia profile (e.g. JSON-LD `sameAs`) must
    # not be mislabeled a directory (hastingsfirm.com regression).
    for sel, attr in (('link[rel="canonical"]', "href"), ('meta[property="og:url"]', "content")):
        node = tree.css_first(sel)
        if node and is_aggregator_domain(_host(node.attributes.get(attr) or "")):
            return "directory_profile"
    low = (html.decode("utf-8", "ignore") if isinstance(html, bytes) else html).lower()
    for name, markers in _PLATFORM_FINGERPRINTS:
        if any(m in low for m in markers):
            return name
    return "custom"


# ---------------------------------------------------------------------------
# Relevance gate (§5.2 / §12.2)


@dataclass
class RelevanceResult:
    is_law_related: bool
    terms: list[str]
    jsonld_types: list[str]


def relevance_gate(html: str | bytes) -> RelevanceResult:
    tree = _tree(html)
    jsonld = _jsonld_types(tree)
    legal_jsonld = [t for t in jsonld if t in _LEGAL_JSONLD_TYPES]
    head = _title_and_head(tree).lower()
    body = _visible_text(tree).lower()[:3000]
    hay = head + " " + body
    terms = [tok for tok in _LEGAL_TOKENS if tok in hay]
    # legal if a legal JSON-LD type OR >=2 text tokens (or 1 token in title)
    is_legal = bool(legal_jsonld) or len(terms) >= 2 or any(t in head for t in _LEGAL_TOKENS)
    return RelevanceResult(is_law_related=is_legal, terms=terms, jsonld_types=jsonld)


# ---------------------------------------------------------------------------
# Headcount cascade (§3, §4, §12.3)


# "N attorneys" phrasings that are NEWS / announcements, not a firm-wide total
# (e.g. Fennemore's "15 Attorneys and Legal Professionals Join" merger headline,
# "six attorneys named to Super Lawyers", "welcomes 3 new associates").
_ANNOUNCE_TRAILING: tuple[str, ...] = (
    "join",
    "named",
    "welcom",
    "elect",
    "promot",
    "honor",
    "recogn",
    "listed",
    "rejoin",
    "appoint",
    "lateral",
    "select",
    "awarded",
    "per state",  # "Top 40 Under 40 ... only 40 attorneys per state" (award quota)
    "per year",
)
_ANNOUNCE_LEADING: tuple[str, ...] = (
    "welcom",
    "expand",
    "announc",
    "congratulat",
    "adds ",
    "adding ",
    "added ",
    "there are ",  # "...there are ~4000 lawyers throughout the nation" (population stat)
)
# Award/ranking words that immediately precede the number ("Top 100 Lawyers",
# "Best 100 Lawyers") — checked as the word right before the count (head suffix),
# NOT anywhere nearby, so "a top IP firm, our 100 attorneys" is NOT rejected.
_AWARD_BEFORE: tuple[str, ...] = ("top", "best", "chapter")

# A stated attorney/lawyer count above this is almost never a single firm's own
# headcount in our universe — it's a statewide/national bar population stat
# ("More than 15,000 lawyers are practicing in Indiana", criminaldefenseteam.com)
# or other comparative figure. The largest single US firms are ~4,000 attorneys;
# rare global networks above this are SPAs we undercount anyway. Staff counts are
# not capped this tightly (a company can have thousands of employees).
_MAX_FIRM_ATTORNEYS = 5000

# Context tokens that mark a "<n> attorneys/lawyers" match as NOT this firm's own
# headcount — a professional network/association, an elite-membership /
# certification population, a statistic, or another firm. Validated against the
# full-run false positives (Mackrell-network "4,500 lawyers worldwide", "Florida
# Bar members ... board certified", "Lawyers Found/Trained", "limited to 250
# attorneys", "DLA Piper has 4,827 attorneys"). Dollar amounts, "attorney fee"
# phrases, and phone/number tails are handled separately below.
_NOT_FIRM_COUNT_CTX: tuple[str, ...] = (
    # shared network / association / org count, not one firm. NB "law firms with"
    # / "firms with" / "firms and" (not bare "law firms") so a firm describing its
    # category — "one of the top IP law firms, our 100 attorneys" (cantorcolburn) —
    # is NOT rejected.
    "network",
    "mackrell",
    "law firms with",
    "firms with",
    "firms and",
    "access to",
    "member firm",
    "attorney members",
    "lawyer members",
    "organization of",
    "organization with",
    "organization made",
    "association of",
    "alliance",
    "made up of",
    "consortium",
    "teams of",
    # elite membership / certification / distinction population
    "board certified",
    "board-certified",
    "certified by",
    "distinction",
    "limited to",
    "fewer than",
    "reserved",
    "academy",
    "designation",
    "credential",
    "diplomate",
    "one of only",
    "one of just",
    "one of approximately",
    "one of fewer",
    "one of the few",
    # client testimonial ("after interviewing 10+ attorneys ...") / negation
    "interviewing",
    "interviewed",
    "spoke with",
    "speaking with",
    "worked with",
    "n't have",
    "not have",
    # statistic / UI / marketing count (not a headcount)
    "trained",
    "readership",
    "newsletter",
    "surveys",
    "peer review",
    "endorsed by",
    "received votes",
    "client list",
    "living the dream",
    "enter the number",
    "evidence code",
    "making it the",
    "not those",
)
# phone tail / number run before (incl. "24/7" and "10/10" via the slash)
_NUM_RUN_BEFORE = re.compile(r"\d[\s.\-()/]{0,4}$")


def _stated_count(
    texts: list[str], pattern: re.Pattern[str], *, max_n: int = 100000
) -> tuple[int, bool, str] | None:
    """Highest plausible '<n> attorneys/lawyers' (or staff) across texts,
    EXCLUDING contexts that aren't this firm's own total: press-release /
    announcement headlines, fees/phones, and professional-network / bar-
    population / statistic mentions. `max_n` caps an implausible value.
    """
    best: tuple[int, bool, str] | None = None
    for text in texts:
        for m in pattern.finditer(text):
            n = _to_int(m.group(1))
            is_min = "+" in m.group(0)
            if _drop_year(n) and not is_min:
                continue
            if n <= 0 or n > max_n:
                continue
            tail = text[m.end() : m.end() + 45].lower()
            head = text[max(0, m.start() - 45) : m.start()].lower()
            # announcement headline ("N attorneys join/named/...", "Top 100 Lawyers")
            if any(w in tail for w in _ANNOUNCE_TRAILING) or any(
                w in head for w in _ANNOUNCE_LEADING
            ):
                continue
            # word immediately before the number marks it as NOT a headcount: a
            # fee ("$5,000 attorneys"), a bankruptcy chapter ("Chapter 7
            # attorney"), or an award ("Top/Best 100 Lawyers"). Plus a fee phrase
            # right after ("5,000 attorney flat fee").
            if head.rstrip().endswith(("$", *_AWARD_BEFORE)) or any(
                w in tail[:16] for w in ("fee", "retainer", "per hour", "hourly")
            ):
                continue
            # phone tail or a longer number run immediately before ("288 - 3888
            # attorney", "0 3253 lawyer") — a digit then optional phone separators
            if _NUM_RUN_BEFORE.search(head) or "found" in tail[:10]:
                continue
            # professional-network / bar-population / statistic / other-firm context
            if any(w in head or w in tail for w in _NOT_FIRM_COUNT_CTX):
                continue
            if best is None or n > best[0]:
                snippet = text[max(0, m.start() - 30) : m.end() + 30].strip()
                best = (n, is_min, snippet)
    return best


def _profile_link_slugs(team_html: str | bytes) -> set[str]:
    """Distinct attorney-profile link slugs on a page (absolute or relative)."""
    tree = _tree(team_html)
    slugs: set[str] = set()
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if _PROFILE_LINK.search(href):
            slugs.add(href.split("#")[0].rstrip("/").lower())
    return slugs


# Substrings that mark a heading as a PAGE TITLE / section / practice area
# rather than a person's name — so we don't count "Phoenix Car Accident
# Attorney" or "Meet Our Attorneys" as people (the Entrekin over-count bug).
_NOT_PERSON_HEADING: tuple[str, ...] = (
    "accident",
    "injury",
    "practice",
    " areas",
    "areas of",
    "welcome",
    "contact",
    "menu",
    "review",
    "verdict",
    "result",
    "service",
    "faq",
    "blog",
    "news",
    "testimonial",
    "why ",
    "meet ",
    "our ",
    "how ",
    "what ",
    "case ",
    "free ",
    "consultation",
    "español",
    "espanol",
    "attorney",
    "lawyer",
    "counsel",
)


_NAME_SUFFIX: frozenset[str] = frozenset(
    {"esq", "esquire", "jr", "sr", "ii", "iii", "iv", "phd", "llm", "md", "cpa", "jd", "mba"}
)


def _looks_like_person(name: str) -> bool:
    """Heading plausibly NAMES a person (2-4 capitalized tokens), not a page
    title / practice-area / section header."""
    low = name.lower()
    if any(bad in low for bad in _NOT_PERSON_HEADING):
        return False
    words = [w for w in name.replace(",", " ").split() if w]
    if not (2 <= len(words) <= 4):
        return False
    # Reject testimonial-style "Firstname L." (single-initial last name): those
    # are review authors, not attorney-roster names (dmvinjurylaw.com counted
    # "Maria A.", "Tony I.", "Eddy Z." as attorneys).
    if len(words[-1].strip(".")) <= 1:
        return False
    alpha = [w for w in words if w[:1].isalpha()]
    return bool(alpha) and all(w[0].isupper() for w in alpha)


def _person_key(name: str) -> str:
    """Normalize a person heading to 'first last' for dedup across case, middle
    initials, and suffixes — so the SAME attorney listed twice ('COLIN M. JONES,
    ESQ.' and 'Colin Jones, Esq.') or repeated on multiple crawled pages is
    counted once, not summed (llflegal.com / wilshirelawfirm.com over-counts)."""
    toks = [
        t
        for t in re.sub(r"[^a-z\s]", " ", name.lower()).split()
        if len(t) > 1 and t not in _NAME_SUFFIX
    ]
    return f"{toks[0]} {toks[-1]}" if len(toks) >= 2 else " ".join(toks)


def _heading_roles(team_html: str | bytes) -> tuple[set[str], set[str]]:
    """Distinct attorney vs staff person-cards: a person-NAME heading whose
    adjacent text carries an attorney- or staff-role keyword. Returns SETS of
    normalized person keys (deduped) so the same person listed twice on a page
    (e.g. an uppercase header + a titlecase card) counts once."""
    tree = _tree(team_html)
    attorneys: set[str] = set()
    staff: set[str] = set()
    for h in tree.css("h2, h3, h4"):
        name = " ".join((h.text() or "").split())
        if not name or len(name) > 60 or looks_like_firm(name) or not _looks_like_person(name):
            continue
        # role text comes from the ADJACENT block (the name itself is gated to
        # exclude role words, so a real role keyword must be in a sibling).
        ctx = name.lower()
        sib = h.next
        hops = 0
        while sib is not None and hops < 3:
            if hasattr(sib, "text"):
                ctx += " " + (sib.text() or "").lower()
            sib = sib.next
            hops += 1
        key = _person_key(name)
        if not key:
            continue
        if any(r in ctx for r in _ATTORNEY_ROLE):
            attorneys.add(key)
        elif any(r in ctx for r in _STAFF_ROLE):
            staff.add(key)
    return attorneys, staff


def _solo_signal(home_html: str | bytes) -> bool:
    tree = _tree(home_html)
    nav_text = " ".join((a.text() or "").lower() for a in tree.css("nav a, header a, ul li a"))
    if any(s in nav_text for s in _SOLO_NAV) and "attorneys" not in nav_text:
        return True
    body = _visible_text(tree).lower()
    return any(fp in body for fp in _FIRST_PERSON)


def extract_headcount(
    pages: list[tuple[str, str | bytes]], base_url: str
) -> tuple[HeadcountResult, int | None]:
    """Run the cascade over (role, html) pages. Returns (attorney result,
    staff_count). Roles are hints: 'home', 'about', 'team', 'attorneys'."""
    texts = [_visible_text(_tree(html)) for _role, html in pages]
    team_pages = [html for role, html in pages if role in ("team", "attorneys", "people")]

    # staff (stated) — same regex family, lower priority
    staff_stated = _stated_count(texts, _STAFF_COUNT)
    staff_count = staff_stated[0] if staff_stated else None

    # 1. stated attorney count (capped: a value above _MAX_FIRM_ATTORNEYS is a
    #    statewide/national bar-population stat, not this firm's headcount)
    stated = _stated_count(texts, _STATED_COUNT, max_n=_MAX_FIRM_ATTORNEYS)
    if stated:
        n, is_min, ev = stated
        return HeadcountResult(n, is_min, "stated", "high", ev), staff_count

    # 2. profile-link count: UNION distinct attorney-profile slugs across ALL
    #    crawled pages (not just team/attorney pages) — handles multi-subpage
    #    rosters (Partners / Associates / Of Counsel on separate pages, as with
    #    Martin & Bonnett) AND firms that link each /attorney/{slug} straight
    #    from the home/about page with no separate roster index discovered
    #    (peterferracuti.com: 3 attorney links on the home page -> previously
    #    fell through to `unknown`). Heading-role classification below stays
    #    team-only, since home-page headings are marketing copy, not people.
    slugs: set[str] = set()
    for _role, html in pages:
        slugs |= _profile_link_slugs(html)
    if len(slugs) >= 2:
        return HeadcountResult(len(slugs), True, "profile_links", "high", None), staff_count

    # 3. heading-role classification — UNION distinct attorney NAMES across team
    #    pages (dedup by first+last), NOT sum of per-page counts, so the same
    #    person in two formats or repeated on multiple crawled roster pages is
    #    counted once (llflegal.com 351->distinct, wilshirelawfirm.com).
    att_names: set[str] = set()
    stf_names: set[str] = set()
    for html in team_pages:
        att, stf = _heading_roles(html)
        att_names |= att
        stf_names |= stf
    stf_names -= att_names  # a name seen as an attorney anywhere is not staff
    if att_names:
        return (
            HeadcountResult(len(att_names), False, "heading_roles", "medium", None),
            staff_count if staff_count is not None else (len(stf_names) or None),
        )

    # 4. solo signal
    home = next((html for role, html in pages if role == "home"), None)
    if home is not None and _solo_signal(home):
        return HeadcountResult(1, False, "solo", "medium", None), staff_count

    # 5. unknown
    return HeadcountResult(None, False, "unknown", "none", None), staff_count


# ---------------------------------------------------------------------------
# Offices, years, phones, signals, scope, description


def extract_offices(html: str | bytes) -> tuple[int | None, list[dict[str, str]]]:
    """Parse footer-style addresses. Footer addresses (often with phones) are
    more reliable than marketing headline office counts (§3.2)."""
    text = _visible_text(_tree(html))
    seen: set[tuple[str, str, str]] = set()
    addrs: list[dict[str, str]] = []
    for m in _CITY_STATE_ZIP.finditer(text):
        # group(1) may carry a leading street/prose fragment from the flat
        # text; keep only the trailing run of Title-cased words (the city),
        # stopping at a street-suffix / unit token ("...Drive East Englewood"
        # -> "East Englewood"; "...2nd Floor Los Angeles" -> "Los Angeles";
        # "...Suite 210-B Bakersfield" -> "Bakersfield").
        city_words: list[str] = []
        for w in reversed(m.group(1).split()):
            wc = w.strip(".,")
            if not wc:
                break
            if wc.lower() in _STREET_STOP or (len(wc) == 1 and wc.isalpha()):
                break  # reached the street / unit / suite-letter part
            if wc[:1].isupper() and wc.replace("-", "").isalpha():
                city_words.insert(0, wc)
            else:
                break
        city = " ".join(city_words[-3:])
        state, postal = m.group(2), m.group(3)
        key = (city.lower(), state, postal)
        if not city or key in seen:
            continue
        seen.add(key)
        addrs.append({"city": city, "state": state, "postal_code": postal})
    # Footer addresses are the most reliable count; fall back to a stated
    # "N offices/locations" in the copy when no addresses parsed (§3.1).
    office_count = len(addrs) or None
    if office_count is None:
        mo = _OFFICE_COUNT.search(text)
        if mo:
            office_count = int(mo.group(1))
    return office_count, addrs


def extract_years(text: str, *, now_year: int | None = None) -> tuple[int | None, bool]:
    now_year = now_year or datetime.now(UTC).year
    low = text.lower()
    if "quarter century" in low:
        return 25, True
    fm = _FOUNDED.search(text)
    if fm:
        yr = int(fm.group(1))
        # Floor at 1780: no US law firm predates ~1790 (Cadwalader, 1792, is the
        # oldest), so an earlier "founded/since YYYY" is a city/historical
        # reference, not the firm ("...North America. Founded in 1764 by French
        # [settlers]" = St. Louis, on missourilawyers.com -> bogus 262 years).
        if 1780 <= yr <= now_year:
            return now_year - yr, False
    best: tuple[int, bool] | None = None
    for m in _YEARS.finditer(text):
        n = int(m.group(1))
        if not (1 <= n <= 200):
            continue
        # "X years of combined/collective experience" is summed across the
        # whole team, NOT the firm's age (thevirgalawfirm.com / sdtriallaw.com
        # both say "100 years of combined/collective experience").
        ctx = low[max(0, m.start() - 12) : m.end() + 30]
        if "combined" in ctx or "collective" in ctx:
            continue
        is_min = "+" in m.group(0)
        if best is None or n > best[0]:
            best = (n, is_min)
    return best if best else (None, False)


def extract_year_founded(text: str, *, now_year: int | None = None) -> int | None:
    """The firm's founding YEAR ('Established in 1947' -> 1947), floored at 1780
    (no US firm predates ~1790; an earlier year is a city/historical reference)."""
    now_year = now_year or datetime.now(UTC).year
    fm = _FOUNDED.search(text)
    if fm:
        yr = int(fm.group(1))
        if 1780 <= yr <= now_year:
            return yr
    return None


# Title separators: pipe, en/em dash, middot, bullet, or " - ". e.g.
# "Firm | Tagline" / "Firm - PI Lawyers".
# � = the Unicode replacement char, which scraped <title>s carry where a real
# separator (en/em-dash, bullet, (TM)/(R)) was mojibake'd — split on it too so a
# real name fused to an SEO descriptor ("Fielding Law<?> Personal Injury Law Firm")
# still separates.
_TITLE_SEP = re.compile(r"\s*[|–—·•�]\s*|\s+-\s+")  # noqa: RUF001 (intentional dash separators)
# Strong firm-name markers — stricter than looks_like_firm (which matches bare
# "Lawyers"), so a practice DESCRIPTOR in a <title>/<h1> ("Personal Injury
# Lawyers") is NOT taken as a name. Trailing spaces on short suffixes avoid the
# " pa"/"Parker" over-match. Tested against the padded, lower-cased candidate.
_STRONG_FIRM: tuple[str, ...] = (
    " llp",
    " lllp",
    " llc",
    " pllc",
    " p.c",
    " pc ",
    " p.a",
    " pa ",
    " apc ",
    " plc ",
    " ltd ",
    " & ",
    " and associates",
    "& associates",
    "law firm",
    "law group",
    "law offices",
    "law office",
    "law center",
    "legal group",
    "legal services",
    # NB: NOT bare " law "/" legal " — those match practice descriptors
    # ("Traffic Law", "Family Law"), which must not be taken as firm names.
)
_GENERIC_NAME: frozenset[str] = frozenset(
    {
        "home",
        "homepage",
        "home page",
        "welcome",
        "contact",
        "contact us",
        "about",
        "about us",
        "menu",
        "untitled",
        "index",
        "blog",
        "our team",
        "attorneys",
        "lawyers",
        "our attorneys",
        # site-builder placeholders (unconfigured Wix/Squarespace/etc.)
        "mysite",
        "my site",
        "site",
        "new site",
        "new page",
        "website",
        "my website",
    }
)


def _clean_name(s: str) -> str:
    # Trim leading/trailing junk — separator punctuation, mojibake replacement chars
    # (a ™/®/dash left clinging to a segment, e.g. "Fielding Law<?>"), stray symbols
    # — codepoint-agnostically (strip anything that isn't a word char), while keeping
    # interior text and a trailing "." / ")" (entity suffixes "P.A.", "(Chikk)").
    s = " ".join((s or "").split())
    s = re.sub(r"^[^\w(]+", "", s)
    s = re.sub(r"[^\w.)]+$", "", s)
    s = re.sub(r"^(welcome to|home)\s+", "", s, flags=re.I)
    return s.strip()


# Generic words that carry no firm IDENTITY. Stripped before deciding whether a
# candidate is merely a descriptor ("Phoenix Law Firm", "Personal Injury Law Firm",
# "Global Law Firm") rather than a real name: entity suffixes, legal-org nouns,
# filler, and size/quality qualifiers.
_NAME_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "of", "a", "an", "at", "for", "your", "our", "is", "in",
        "law", "laws", "firm", "firms", "office", "offices", "group", "groups",
        "center", "centers", "practice", "practices",
        "attorney", "attorneys", "lawyer", "lawyers", "counsel", "esq",
        "legal", "services", "service", "associates", "association", "partners",
        "blog", "blawg", "news", "home", "homepage", "website",
        "llp", "lllp", "llc", "pllc", "pc", "pa", "apc", "plc", "ltd", "co", "inc",
        "skilled", "experienced", "trusted", "local", "affordable", "aggressive",
        "best", "top", "premier", "leading", "global", "national", "nationwide",
        "international", "statewide", "regional", "online",
    }
)

# Entity-suffix / "&" markers — a STRONG signal a candidate is a real firm name (a
# subset of _STRONG_FIRM; "law firm"/"law office" are weaker, descriptor-prone).
_ENTITY_SUFFIX_MARK: tuple[str, ...] = (
    " llp", " lllp", " llc", " pllc", " p.c", " pc ", " p.a", " pa ",
    " apc ", " plc ", " ltd ", " & ", " and associates", "& associates",
)


def _firm_name_core(name: str) -> list[str]:
    """Distinctive (identity-bearing) tokens of a name — alphabetic tokens with the
    generic legal / structural / qualifier words removed."""
    return [
        t for t in re.findall(r"[a-z]+", name.lower()) if len(t) > 1 and t not in _NAME_STOPWORDS
    ]


# Observed non-firm titles that pass the relevance gate but are never a firm's
# name: parked / spam / hijacked-domain CMS defaults, legal blogs/news brands,
# domain-parking services, and non-firm legal entities (law schools). Frequency is
# the tell — these recur across unrelated domains (e.g. "poring168" on 15+).
_NON_FIRM_NAMES: frozenset[str] = frozenset(
    {
        "poring168",
        "teepublic",
        "spaceship",
        "idlix",
        "live draw sgp",
        "unstoppable domains",
        "burgundy today",
        "default",
        "law thinker",
        "school of law",
        "untitled document",
        "index of",
    }
)


def _is_generic_firm_name(name: str) -> bool:
    """True when `name` is a generic descriptor, not a firm's identity: a known
    non-firm/placeholder title (Wix / domain-parking / template / spam), nothing
    distinctive left after dropping generic words ("Law Firm", "Legal Services"), or
    a practice-area descriptor — either a single phrase ("Personal Injury Law Firm",
    "Immigration Law Firm") or a multi-word list of practice areas ("Divorce Family
    Law", "Wills Trusts Estates"). City descriptors ("Phoenix Law Firm") are NOT
    flagged here — the entity-suffix / domain-consistency ranking in
    extract_firm_name demotes those instead.
    """
    low = " ".join((name or "").lower().split())
    low_nodigit = re.sub(r"\s*\d+$", "", low)  # "mysite 1" -> "mysite"
    if low in _GENERIC_NAME or low_nodigit in _GENERIC_NAME or low in _NON_FIRM_NAMES:
        return True
    if "template" in low or "hugedomains" in low or "godaddy" in low:
        return True  # site-builder / domain-parking placeholders
    core = _firm_name_core(name)
    if not core:
        return True
    tax = get_taxonomy()
    if tax.match(" ".join(core)) is not None:
        return True
    # A multi-word name whose every distinctive token is itself a practice area is a
    # descriptor list ("Divorce Family Law", "Accident Injury Attorneys"), not a name.
    return len(core) >= 2 and all(tax.match(t) for t in core)


def _domain_consistent(name: str, host: str) -> bool:
    """A distinctive name token (>=4 chars) appears in the domain host. Real firms'
    domains usually echo their name (Fielding -> fieldinglawfirm.com), so this
    separates the real name from a co-occurring SEO descriptor."""
    if not host:
        return False
    stem = host.split(".")[0].replace("-", "")
    return any(len(t) >= 4 and t in stem for t in _firm_name_core(name))


def _has_entity_marker(c: str) -> bool:
    return any(mk in f" {c.lower()} " for mk in _ENTITY_SUFFIX_MARK)


def extract_firm_name(
    pages: list[tuple[str, str | bytes]], *, base_url: str = ""
) -> tuple[str | None, str | None]:
    """Firm name from the site, preferring STRUCTURED identity (legal JSON-LD
    `name`, og:site_name) over the <title>/<h1>; title/H1 candidates must carry a
    strong firm marker so a practice descriptor isn't mistaken for a name.
    Returns (raw, normalized) — names the nameless Justia-only firms in fusion.
    """
    if not pages:
        return None, None
    tree = _tree(next((h for r, h in pages if r == "home"), pages[0][1]))
    trusted: list[str] = []  # legal JSON-LD name + og:site_name (accept as-is)
    weak: list[str] = []  # <title> / <h1> (require a strong firm marker)
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except (json.JSONDecodeError, ValueError):
            continue
        for obj in data if isinstance(data, list) else [data]:
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type")
            types = [
                x.strip().lower() for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)
            ]
            if isinstance(obj.get("name"), str) and any(ty in _LEGAL_JSONLD_TYPES for ty in types):
                trusted.append(obj["name"])
    og = tree.css_first('meta[property="og:site_name"]')
    if og and og.attributes.get("content"):
        trusted.append(og.attributes["content"])
    ti = tree.css_first("title")
    if ti and ti.text():
        weak.append(ti.text())
    h1 = tree.css_first("h1")
    if h1 and h1.text():
        weak.append(h1.text())

    def _segments(strings: list[str]) -> list[str]:
        # Split each candidate on title separators (a junk og:site_name like
        # "Jones Walker LLP - Jones Walker LLP | Homepage" -> "Jones Walker LLP"),
        # clean, and drop generic descriptors / placeholders / out-of-range lengths.
        out: list[str] = []
        for s in strings:
            for seg in _TITLE_SEP.split(s):
                c = _clean_name(seg)
                if c and 2 <= len(c) <= 80 and not _is_generic_firm_name(c) and c not in out:
                    out.append(c)
        return out

    def _has_marker(c: str) -> bool:
        return any(mk in f" {c.lower()} " for mk in _STRONG_FIRM)

    # Score every non-generic candidate and pick the best (ties -> earliest by source
    # order). A real firm name beats a co-occurring SEO descriptor because it carries
    # an entity suffix (PLLC / P.A. / &) and/or echoes the domain, whereas "Phoenix
    # Law Firm" / "Personal Injury Law Firm" carry only a weak descriptor marker and
    # don't match the host. Weak (<title>/<h1>) candidates still must look like a firm
    # name (carry some marker) to be considered at all.
    host = _host(base_url)
    scored: list[tuple[int, int, str, str]] = []
    order = 0
    for segs, require_marker, trusted_bonus in (
        (_segments(trusted), False, 2),
        (_segments(weak), True, 0),
    ):
        for c in segs:
            order += 1
            entity = _has_entity_marker(c)
            weak_marker = _has_marker(c)
            if require_marker and not (entity or weak_marker):
                continue
            nn = normalize_firm_name(c)
            if not (nn and nn.normalized):
                continue
            score = trusted_bonus + (3 if entity else 1 if weak_marker else 0)
            if _domain_consistent(c, host):
                score += 3
            scored.append((score, -order, c, nn.normalized))
    if not scored:
        return None, None
    scored.sort(reverse=True)
    return scored[0][2], scored[0][3]


def extract_phones(html: str | bytes) -> list[str]:
    tree = _tree(html)
    raw: list[str] = []
    for a in tree.css('a[href^="tel:"]'):
        raw.append((a.attributes.get("href") or "")[4:])
    for m in _PHONE.finditer(_visible_text(tree)):
        raw.append(m.group(0))
    out: list[str] = []
    for r in raw:
        e164 = normalize_phone(r)
        if e164 and e164 not in out:
            out.append(e164)
    return out


def extract_notable_signals(text: str) -> list[str]:
    low = text.lower()
    return [label for needle, label in _NOTABLE if needle in low]


# A link path that indicates a practice-area page (a sub-segment must follow).
_PRACTICE_PATH = re.compile(
    r"/(?:practice-areas?|areas?-of-practice|our-practices?|practice|services?|"
    r"what-we-do|expertise)/",
    re.I,
)

# Section-landing segments (the /practice-areas/ index itself, not a specific area).
# A trailing path segment equal to one of these is the listing page, not a practice
# area, so it must not be recorded as an unmatched specialty.
_GENERIC_PRACTICE_SEG = frozenset(
    {
        "practice areas",
        "practice area",
        "areas of practice",
        "area of practice",
        "our practices",
        "our practice",
        "practice",
        "services",
        "service",
        "what we do",
        "expertise",
    }
)


def extract_practice_areas(
    pages: list[tuple[str, str | bytes]], *, base_url: str = ""
) -> tuple[list[str], list[str], list[str]]:
    """Legal specialties the firm advertises, matched to the canonical taxonomy.

    Returns ``(matched_slugs, raw_phrases, unmatched)``. Candidates are internal-link
    anchor texts plus the trailing slug of any ``/practice(-areas)/{slug}`` path; each
    is matched via ``get_taxonomy().match`` (exact-on-normalized). Only real legal
    practice areas survive the match, so office LOCATIONS — city / "we serve X" /
    jurisdiction links — can NEVER appear here (this field is the firm's specialty,
    not where it sits or where its lawyers are licensed). ``raw`` keeps the verbatim
    phrase that produced each matched slug, for audit. ``unmatched`` collects practice
    areas the firm DECLARES via a ``/practice-areas/{slug}`` URL but the taxonomy does
    not yet recognize — high precision (the URL structure asserts it is a practice
    area), so anchor-text nav links never leak in; it surfaces taxonomy gaps without
    polluting the matched set.
    """
    tax = get_taxonomy()
    host = _host(base_url)
    slugs: list[str] = []
    raw: list[str] = []
    unmatched: list[str] = []
    seen_raw: set[str] = set()
    seen_unmatched: set[str] = set()
    for _role, html in pages:
        for a in _tree(html).css("a[href]"):
            href = a.attributes.get("href") or ""
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            joined = safe_urljoin(base_url, href)
            if joined is None:
                continue
            parsed = safe_urlparse(joined)
            if parsed and host and parsed.netloc and parsed.netloc.lower() != host:
                continue  # external link
            # Anchor text is a weak candidate; a /practice-areas/{slug} URL segment is
            # a strong one. Only the URL-declared segment may feed `unmatched` (anchor
            # nav text is far too noisy to treat as a taxonomy gap).
            text_cand = " ".join((a.text() or "").split()).strip()
            path = (parsed.path if parsed else "") or ""
            path_seg: str | None = None
            if _PRACTICE_PATH.search(path):
                seg = path.split("#")[0].split("?")[0].rstrip("/").split("/")[-1]
                seg = seg.replace("-", " ").replace("_", " ").strip()
                if seg.lower() not in _GENERIC_PRACTICE_SEG:  # skip the listing page
                    path_seg = seg
            for cand, url_declared in ((text_cand, False), (path_seg, True)):
                if not cand or len(cand) > 60:
                    continue
                slug = tax.match(cand)
                if slug:
                    if cand.lower() not in seen_raw:
                        seen_raw.add(cand.lower())
                        raw.append(cand)
                    if slug not in slugs:
                        slugs.append(slug)
                elif url_declared and cand.lower() not in seen_unmatched:
                    seen_unmatched.add(cand.lower())
                    unmatched.append(cand)
    return slugs, raw, unmatched


def extract_scope(text: str) -> str | None:
    low = text.lower()
    if "national law firm" in low or "nationwide" in low or "across the country" in low:
        return "national"
    if "regional" in low or "multi-state" in low:
        return "regional"
    if "statewide" in low:
        return "state"
    return None


def extract_description_blurb(pages: list[tuple[str, str | bytes]]) -> str | None:
    """First 2-3 sentences of the about page (or home), raw text — the input
    for a later LLM/template description pass (§7, §10)."""
    order = {"about": 0, "home": 1, "team": 2}
    for _role, html in sorted(pages, key=lambda p: order.get(p[0], 9)):
        text = _visible_text(_tree(html))
        if len(text) < 80:
            continue
        sentences = re.split(r"(?<=[.!?])\s+", text)
        blurb = " ".join(sentences[:3]).strip()
        if len(blurb) >= 80:
            return blurb[:600]
    return None


# Headings whose section is navigation / listing / boilerplate, not firm prose.
# A heading containing any of these is dropped (with its body copy) from
# firm_descriptions so we keep only genuine self-description.
_DESC_HEADING_SKIP = (
    "menu",
    "navigation",
    "search",
    "contact",
    "follow us",
    "newsletter",
    "subscribe",
    "sign up",
    "practice area",
    "areas of practice",
    "our practice",
    "what we do",
    "testimonial",
    "review",
    "blog",
    "news",
    "recent post",
    "categories",
    "office hours",
    "directions",
    "find us",
    "social",
    "copyright",
)


def extract_firm_descriptions(
    pages: list[tuple[str, str | bytes]],
) -> list[dict[str, str | None]]:
    """Structured descriptive sections — ``[{heading, text}]`` — from the about
    (else home) page. Each heading (h1-h3) pairs with the body copy that follows it
    in document order, up to the next heading. Distinct from
    ``firm_short_description`` (the single lead blurb): this is the firm's fuller
    self-description, section by section, for downstream summarization. Nav /
    listing / boilerplate headings are dropped and only substantive prose
    (>= 60 chars) is kept; leading copy with no heading is recorded with
    ``heading=None``. No network.
    """
    order = {"about": 0, "home": 1}
    chosen: str | bytes | None = None
    for role, html in sorted(pages, key=lambda p: order.get(p[0], 9)):
        if role in order:
            chosen = html
            break
    if chosen is None:
        return []

    sections: list[dict[str, str | None]] = []
    heading: str | None = None
    buf: list[str] = []

    def _flush() -> None:
        text = re.sub(r"\s+", " ", " ".join(buf)).strip()
        skip = bool(heading) and any(s in heading.lower() for s in _DESC_HEADING_SKIP)
        if len(text) >= 60 and not skip:
            sections.append({"heading": heading, "text": text[:800]})

    tree = _tree(chosen)
    # selectolax css() groups matches by selector, NOT document order, so walk the
    # tree in document (pre-order) order to keep each heading with the prose that
    # follows it (same rationale as _heading_roles' sibling-walk).
    for node in (tree.body or tree.root).traverse(include_text=False):
        tag = node.tag
        if tag not in ("h1", "h2", "h3", "p"):
            continue
        txt = " ".join((node.text() or "").split())
        if tag in ("h1", "h2", "h3"):
            _flush()
            heading = txt or None
            buf = []
        elif txt:
            buf.append(txt)
    _flush()
    return sections[:8]


# Page-copy signals that a firm is defunct: 'closed' (shut down) / 'parked'
# (domain for-sale / placeholder). Mirrors FirmSourceRecord.deactivation_status.
_DEACTIVATION_SIGNALS: tuple[tuple[str, str], ...] = (
    ("permanently closed", "closed"),
    ("no longer in business", "closed"),
    ("has closed its doors", "closed"),
    ("the firm has closed", "closed"),
    ("no longer accepting clients", "closed"),
    ("this domain is for sale", "parked"),
    ("buy this domain", "parked"),
    ("domain is for sale", "parked"),
    ("this site is parked", "parked"),
    ("site is parked", "parked"),
    ("getting things ready", "parked"),
)


def extract_deactivation_status(text: str) -> str | None:
    """Defunct-firm signal from page copy: 'closed' / 'parked' / None."""
    low = text.lower()
    for needle, status in _DEACTIVATION_SIGNALS:
        if needle in low:
            return status
    return None


def extract_contacts(
    pages: list[tuple[str, str | bytes]], *, base_url: str = ""
) -> list[dict[str, str | None]]:
    """Attorneys on the firm's team/attorney pages: ``[{name_raw,
    name_normalized, title}]``, deduped by person (a subset of the FSR contact
    shape). Same person-heading + attorney-role classification as the heading-
    role headcount, so client testimonials / staff are excluded the same way.
    """
    out: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for role, html in pages:
        if role not in ("team", "attorneys", "people"):
            continue
        for h in _tree(html).css("h2, h3, h4"):
            name = " ".join((h.text() or "").split())
            if not name or len(name) > 60 or looks_like_firm(name) or not _looks_like_person(name):
                continue
            ctx = ""
            sib = h.next
            hops = 0
            while sib is not None and hops < 3:
                if hasattr(sib, "text"):
                    ctx += " " + (sib.text() or "")
                sib = sib.next
                hops += 1
            if not any(r in f"{name} {ctx}".lower() for r in _ATTORNEY_ROLE):
                continue
            key = _person_key(name)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "name_raw": name,
                    "name_normalized": key,
                    "title": " ".join(ctx.split())[:80] or None,
                }
            )
    return out


# ---------------------------------------------------------------------------
# Page discovery (which internal pages to fetch beyond the home page)

_NAV_KEYWORDS: dict[str, tuple[str, ...]] = {
    "about": ("about", "our firm", "the firm", "who we are", "our story", "firm overview"),
    "team": ("our team", "meet the team", "our people"),
    "attorneys": (
        "attorney",
        "attorneys",
        "our attorneys",
        "lawyer",
        "lawyers",
        "our lawyers",
        "professionals",
        "our attorney",
    ),
}
_KNOWN_PATHS: dict[str, tuple[str, ...]] = {
    "about": ("/about", "/about-us", "/our-firm", "/firm", "/the-firm"),
    "team": ("/our-team", "/team", "/our-people", "/people"),
    "attorneys": ("/attorneys", "/our-attorneys", "/lawyers", "/our-attorney", "/attorney"),
}
# Pages that are NOT firm-identity/headcount content (skip when discovering).
_SKIP_PATH = re.compile(
    r"/(blog|news|press|insights?|articles?|events?|contact|careers?|jobs|"
    r"privacy|disclaimer|terms|sitemap|search|login|payment|pay-?bill)\b",
    re.I,
)


def discover_internal_pages(
    home_html: str | bytes, base_url: str, *, max_per_role: int = 4
) -> dict[str, list[str]]:
    """From the home page, find same-host about / team / attorney URLs to fetch.

    Nav-text + href-keyword scan first (the reliable path per §12.4), then
    ordered known-path guesses appended as fallbacks. Returns {role: [urls]}
    with nav hits before guesses; the caller decides how many to actually fetch.
    Collects up to `max_per_role` attorney links so multi-subpage rosters
    (Partners/Associates/...) are all captured.
    """
    tree = _tree(home_html)
    host = _host(base_url)
    out: dict[str, list[str]] = {"about": [], "team": [], "attorneys": []}
    seen: set[str] = set()

    def _add(role: str, url: str) -> None:
        key = url.split("#")[0].split("?")[0].rstrip("/")
        if key and key not in seen and len(out[role]) < max_per_role:
            seen.add(key)
            out[role].append(key)

    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        text = " ".join((a.text() or "").split()).lower()
        full = safe_urljoin(base_url, href)
        parsed = safe_urlparse(full) if full else None
        if parsed is None or parsed.scheme not in ("http", "https"):
            continue
        if host and parsed.netloc and parsed.netloc.lower() != host:
            continue  # external link
        path_l = (parsed.path or "").lower()
        if _SKIP_PATH.search(path_l):
            continue
        for role, kws in _NAV_KEYWORDS.items():
            known = tuple(p.strip("/") for p in _KNOWN_PATHS[role])
            if any(k in text for k in kws) or any(path_l.rstrip("/").endswith(k) for k in known):
                _add(role, full)
                break

    # Ordered known-path guesses as fallbacks (worker tries these if nav missed).
    for role, paths in _KNOWN_PATHS.items():
        for path in paths:
            joined = safe_urljoin(base_url, path)
            if joined:
                _add(role, joined)
    return out


# ---------------------------------------------------------------------------
# Compose


def extract_site(
    pages: list[tuple[str, str | bytes]],
    *,
    base_url: str = "",
    now_year: int | None = None,
) -> SiteExtraction:
    """Run the full cascade over fetched (role, html) pages and compose a
    SiteExtraction. `pages` should include at least ('home', html); about/team
    pages improve headcount. No network here.
    """
    if not pages:
        return SiteExtraction(url_verification_status="unreachable", needs_render=True)

    home_html = next((h for r, h in pages if r == "home"), pages[0][1])
    all_text = " ".join(_visible_text(_tree(h)) for _r, h in pages)

    # Relevance over ALL fetched pages, not just home: a sparse Wix/Squarespace
    # home (e.g. TEPLG) can carry <2 legal tokens while the team/about page is
    # clearly a law firm. OR the gate across pages; union the matched terms.
    rels = [relevance_gate(h) for _r, h in pages]
    is_law_related = any(r.is_law_related for r in rels)
    relevance_terms = sorted({t for r in rels for t in r.terms})
    headcount, staff = extract_headcount(pages, base_url)
    office_count, addresses = extract_offices(home_html)
    years, years_min = extract_years(all_text, now_year=now_year)
    practice_areas, practice_areas_raw, practice_areas_unmatched = extract_practice_areas(
        pages, base_url=base_url
    )
    name_raw, name_normalized = extract_firm_name(pages, base_url=base_url)
    blurb = extract_description_blurb(pages)
    # Primary office = the first zip-anchored footer address (HQ, by document
    # order) — a real location, never inferred from "we serve"/jurisdiction copy.
    primary_city = addresses[0]["city"] if addresses else None
    primary_state = addresses[0]["state"] if addresses else None
    primary_postal = addresses[0]["postal_code"] if addresses else None

    out = SiteExtraction(
        platform=detect_platform(home_html),
        is_law_related=is_law_related,
        relevance_terms=relevance_terms,
        name_raw=name_raw,
        name_normalized=name_normalized,
        attorney_count=headcount.count,
        attorney_count_is_min=headcount.is_min,
        attorney_count_method=headcount.method,
        attorney_count_confidence=headcount.confidence,
        attorney_count_raw=headcount.evidence,
        staff_count=staff,
        office_count=office_count,
        office_addresses=addresses,
        primary_city=primary_city,
        primary_state=primary_state,
        primary_postal_code=primary_postal,
        years_in_operation=years,
        years_is_min=years_min,
        year_founded=extract_year_founded(all_text, now_year=now_year),
        phones=extract_phones(home_html),
        contacts=extract_contacts(pages, base_url=base_url),
        notable_signals=extract_notable_signals(all_text),
        practice_areas=practice_areas,
        practice_areas_raw=practice_areas_raw,
        practice_areas_unmatched=practice_areas_unmatched,
        scope=extract_scope(all_text),
        description_blurb=blurb,
        firm_short_description=blurb,
        firm_descriptions=extract_firm_descriptions(pages),
        deactivation_status=extract_deactivation_status(all_text),
    )
    # thin page with nothing extracted -> headless candidate
    out.needs_render = len(all_text) < 400 and out.attorney_count is None
    if is_law_related:
        out.url_verification_status = "verified"
    elif out.needs_render:
        # Too little readable text to judge — do NOT assert "not a law firm" from
        # a JS shell / near-empty page (beaverlawoffice.com = 0 chars,
        # dankolawllc.com = 114). Leave it unverified and flag needs_render for
        # the headless lever; not_a_law_firm is reserved for pages we actually
        # read and found non-legal (swissbiologic, rlb.com).
        out.url_verification_status = "unverified"
    else:
        out.url_verification_status = "not_a_law_firm"
    # .gov / .edu hosts are government offices / clinics, not private firms
    # (e.g. azag.gov = AZ Attorney General) — flag rather than count as a firm.
    host = _host(base_url)
    if host.endswith((".gov", ".edu")):
        out.url_verification_status = "government_or_edu"
    return out


__all__ = [
    "HeadcountResult",
    "RelevanceResult",
    "SiteExtraction",
    "detect_platform",
    "discover_internal_pages",
    "extract_contacts",
    "extract_deactivation_status",
    "extract_firm_name",
    "extract_headcount",
    "extract_offices",
    "extract_phones",
    "extract_practice_areas",
    "extract_site",
    "extract_year_founded",
    "extract_years",
    "relevance_gate",
]
