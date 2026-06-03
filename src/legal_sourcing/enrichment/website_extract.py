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
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

from legal_sourcing.normalize.name import looks_like_firm
from legal_sourcing.normalize.phone import normalize_phone

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
    {"legalservice", "attorney", "lawyer", "legalservices"}
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
    # order matters: directory-profile first (it's NOT the firm's own site)
    ("directory_profile", ("findlaw.com/lawfirm", "martindale.com", "justia.com", "avvo.com")),
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

_STATED_COUNT = re.compile(r"(\d[\d,]{0,6})\s*\+?\s*(attorneys?|lawyers?)\b", re.I)
_STAFF_COUNT = re.compile(
    r"(\d[\d,]{0,6})\s*\+?\s*(staff|employees|professionals|team members)\b", re.I
)
_OFFICE_COUNT = re.compile(r"(\d{1,3})\s*\+?\s*(offices?|locations?)\b", re.I)
_YEARS = re.compile(r"(\d{1,3})\s*\+?\s*(?:years?|yrs?)\b", re.I)
_FOUNDED = re.compile(r"(?:founded|established|since|serving\D{0,20}since)\D{0,12}(\d{4})", re.I)
_PHONE = re.compile(r"\(?\b\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b")
_CITY_STATE_ZIP = re.compile(r"([A-Za-z][A-Za-z.\s]{1,38}?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?")
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
    attorney_count: int | None = None
    attorney_count_is_min: bool = False
    attorney_count_method: str = "unknown"
    attorney_count_confidence: str = "none"
    attorney_count_raw: str | None = None
    staff_count: int | None = None
    office_count: int | None = None
    office_addresses: list[dict[str, str]] = field(default_factory=list)
    years_in_operation: int | None = None
    years_is_min: bool = False
    phones: list[str] = field(default_factory=list)
    notable_signals: list[str] = field(default_factory=list)
    scope: str | None = None
    description_blurb: str | None = None
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


# ---------------------------------------------------------------------------
# Platform


def detect_platform(html: str | bytes) -> str:
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
)
_ANNOUNCE_LEADING: tuple[str, ...] = (
    "welcom",
    "expand",
    "announc",
    "congratulat",
    "adds ",
    "adding ",
    "added ",
)


def _stated_count(texts: list[str], pattern: re.Pattern[str]) -> tuple[int, bool, str] | None:
    """Highest plausible '<n> attorneys/lawyers' (or staff) across texts,
    EXCLUDING press-release / announcement contexts that aren't a firm total."""
    best: tuple[int, bool, str] | None = None
    for text in texts:
        for m in pattern.finditer(text):
            n = _to_int(m.group(1))
            is_min = "+" in m.group(0)
            if _drop_year(n) and not is_min:
                continue
            if n <= 0 or n > 100000:
                continue
            tail = text[m.end() : m.end() + 30].lower()
            head = text[max(0, m.start() - 30) : m.start()].lower()
            if any(w in tail for w in _ANNOUNCE_TRAILING) or any(
                w in head for w in _ANNOUNCE_LEADING
            ):
                continue  # a "N attorneys join/named/..." headline, not a total
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


def _looks_like_person(name: str) -> bool:
    """Heading plausibly NAMES a person (2-4 capitalized tokens), not a page
    title / practice-area / section header."""
    low = name.lower()
    if any(bad in low for bad in _NOT_PERSON_HEADING):
        return False
    words = [w for w in name.replace(",", " ").split() if w]
    if not (2 <= len(words) <= 4):
        return False
    alpha = [w for w in words if w[:1].isalpha()]
    return bool(alpha) and all(w[0].isupper() for w in alpha)


def _heading_roles(team_html: str | bytes) -> tuple[int, int]:
    """Count attorney vs staff person-cards: a person-NAME heading whose
    adjacent text carries an attorney- or staff-role keyword."""
    tree = _tree(team_html)
    attorneys = 0
    staff = 0
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
        if any(r in ctx for r in _ATTORNEY_ROLE):
            attorneys += 1
        elif any(r in ctx for r in _STAFF_ROLE):
            staff += 1
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

    # 1. stated attorney count
    stated = _stated_count(texts, _STATED_COUNT)
    if stated:
        n, is_min, ev = stated
        return HeadcountResult(n, is_min, "stated", "high", ev), staff_count

    # 2. profile-link count: UNION distinct attorney-profile slugs across ALL
    #    team/attorney pages — handles multi-subpage rosters (e.g. Partners /
    #    Associates / Of Counsel on separate pages, as with Martin & Bonnett).
    slugs: set[str] = set()
    for html in team_pages:
        slugs |= _profile_link_slugs(html)
    if len(slugs) >= 2:
        return HeadcountResult(len(slugs), True, "profile_links", "high", None), staff_count

    # 3. heading-role classification, summed across all team pages
    att_total = 0
    stf_total = 0
    for html in team_pages:
        att, stf = _heading_roles(html)
        att_total += att
        stf_total += stf
    if att_total >= 1:
        return (
            HeadcountResult(att_total, False, "heading_roles", "medium", None),
            staff_count if staff_count is not None else (stf_total or None),
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
        # text; keep only the trailing run of Title-cased words (the city).
        city_words: list[str] = []
        for w in reversed(m.group(1).split()):
            wc = w.strip(".,")
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
        if 1700 <= yr <= now_year:
            return now_year - yr, False
    best: tuple[int, bool] | None = None
    for m in _YEARS.finditer(text):
        n = int(m.group(1))
        if 1 <= n <= 200:
            is_min = "+" in m.group(0)
            if best is None or n > best[0]:
                best = (n, is_min)
    return best if best else (None, False)


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
    host = (urlparse(base_url).netloc or "").lower()
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
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https"):
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
            _add(role, urljoin(base_url, path))
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

    out = SiteExtraction(
        platform=detect_platform(home_html),
        is_law_related=is_law_related,
        relevance_terms=relevance_terms,
        attorney_count=headcount.count,
        attorney_count_is_min=headcount.is_min,
        attorney_count_method=headcount.method,
        attorney_count_confidence=headcount.confidence,
        attorney_count_raw=headcount.evidence,
        staff_count=staff,
        office_count=office_count,
        office_addresses=addresses,
        years_in_operation=years,
        years_is_min=years_min,
        phones=extract_phones(home_html),
        notable_signals=extract_notable_signals(all_text),
        scope=extract_scope(all_text),
        description_blurb=extract_description_blurb(pages),
    )
    # thin page with nothing extracted -> headless candidate
    out.needs_render = len(all_text) < 400 and out.attorney_count is None
    out.url_verification_status = "not_a_law_firm" if not is_law_related else "verified"
    # .gov / .edu hosts are government offices / clinics, not private firms
    # (e.g. azag.gov = AZ Attorney General) — flag rather than count as a firm.
    host = (urlparse(base_url).netloc or "").lower()
    if host.endswith((".gov", ".edu")):
        out.url_verification_status = "government_or_edu"
    return out


__all__ = [
    "HeadcountResult",
    "RelevanceResult",
    "SiteExtraction",
    "detect_platform",
    "discover_internal_pages",
    "extract_headcount",
    "extract_offices",
    "extract_phones",
    "extract_site",
    "extract_years",
    "relevance_gate",
]
