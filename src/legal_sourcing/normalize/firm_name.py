"""Shared firm-NAME quality predicate — the single source of truth for "is this a
usable firm identity, or generic/placeholder/descriptor junk?".

This logic was developed and validated inside the website extractor
(``enrichment/website_extract.py``); it is lifted here verbatim so that all three
consumers share ONE definition instead of re-implementing the heuristic (which would
inevitably diverge):

  * website extraction — when choosing the firm name from a site,
  * cross-source name cleanup (Fixer) — flagging generic/junk names on
    martindale/az_bar/findlaw rows for recovery,
  * canonical resolution (Canonizer) — a defense-in-depth guard so a generic name
    ("Phoenix Law Firm") is never treated as a strong merge key in the
    name+city+state floor (two unrelated such rows must not false-merge).

POLICY NOTE: a ``True`` result means "this name is not a reliable identity — find a
better one and RENAME the row" (recover from another signal: cached HTML, domain echo,
logo alt-text). NULL the name only as a last resort, and KEEP the firm row either way.
A low-quality name is never, by itself, a reason to drop a firm.
"""

from __future__ import annotations

import re

from legal_sourcing.normalize.practice_areas import get_taxonomy

# Unconfigured-site / navigation / placeholder titles that carry no firm identity.
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

# Generic words that carry no firm IDENTITY. Stripped before deciding whether a
# candidate is merely a descriptor ("Phoenix Law Firm", "Personal Injury Law Firm")
# rather than a real name: entity suffixes, legal-org nouns, filler, size/quality
# qualifiers.
_NAME_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "of",
        "a",
        "an",
        "at",
        "for",
        "your",
        "our",
        "is",
        "in",
        "law",
        "laws",
        "firm",
        "firms",
        "office",
        "offices",
        "group",
        "groups",
        "center",
        "centers",
        "practice",
        "practices",
        "attorney",
        "attorneys",
        "lawyer",
        "lawyers",
        "counsel",
        "esq",
        "legal",
        "services",
        "service",
        "associates",
        "association",
        "partners",
        "blog",
        "blawg",
        "news",
        "llp",
        "lllp",
        "llc",
        "pllc",
        "pc",
        "pa",
        "apc",
        "plc",
        "ltd",
        "co",
        "inc",
        "skilled",
        "experienced",
        "trusted",
        "local",
        "affordable",
        "aggressive",
        "best",
        "top",
        "premier",
        "leading",
        "global",
        "national",
        "nationwide",
        "international",
        "statewide",
        "regional",
        "online",
    }
)

# Entity-suffix / "&" markers — a STRONG signal a candidate is a real firm name.
_ENTITY_SUFFIX_MARK: tuple[str, ...] = (
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
)

# Observed non-firm titles that pass a relevance gate but are never a firm's name:
# parked / spam / hijacked-domain CMS defaults, legal blogs/news brands, domain
# parking, and non-firm legal entities (law schools). Frequency is the tell — these
# recur across unrelated domains (e.g. "poring168" on 15+).
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

# US state names + distinctive city tokens — used only to spot LOCATION SEO
# descriptors ("Georgia Nursing Home Abuse Lawyers"). Multi-word places reduce to the
# distinctive token ("new"/"north"/"south"/"west" are stopwords). The geo rule
# requires a practice-area remainder too, so a bare place / surname ("Texas Law") is
# never flagged on this basis.
_GEO_TERMS: frozenset[str] = frozenset(
    {
        "alabama",
        "alaska",
        "arizona",
        "arkansas",
        "california",
        "colorado",
        "connecticut",
        "delaware",
        "florida",
        "georgia",
        "hawaii",
        "idaho",
        "illinois",
        "indiana",
        "iowa",
        "kansas",
        "kentucky",
        "louisiana",
        "maine",
        "maryland",
        "massachusetts",
        "michigan",
        "minnesota",
        "mississippi",
        "missouri",
        "montana",
        "nebraska",
        "nevada",
        "hampshire",
        "jersey",
        "mexico",
        "carolina",
        "dakota",
        "ohio",
        "oklahoma",
        "oregon",
        "pennsylvania",
        "rhode",
        "tennessee",
        "texas",
        "utah",
        "vermont",
        "virginia",
        "wisconsin",
        "wyoming",
        "phoenix",
        "tucson",
        "dallas",
        "houston",
        "antonio",
        "miami",
        "orlando",
        "tampa",
        "jacksonville",
        "atlanta",
        "denver",
        "seattle",
        "portland",
        "philadelphia",
        "pittsburgh",
        "detroit",
        "cleveland",
        "columbus",
        "indianapolis",
        "nashville",
        "memphis",
        "louisville",
        "charlotte",
        "raleigh",
        "vegas",
        "angeles",
        "diego",
        "francisco",
        "sacramento",
        "fresno",
        "brooklyn",
        "baltimore",
        "richmond",
        "norfolk",
        "savannah",
        "orleans",
        "birmingham",
        "minneapolis",
        "milwaukee",
        "omaha",
        "tulsa",
        "albuquerque",
        "boise",
        "spokane",
    }
)


def firm_name_core(name: str) -> list[str]:
    """Distinctive (identity-bearing) tokens of a name — alphabetic tokens with the
    generic legal / structural / qualifier words removed."""
    return [
        t
        for t in re.findall(r"[a-z]+", (name or "").lower())
        if len(t) > 1 and t not in _NAME_STOPWORDS
    ]


def is_generic_firm_name(name: str | None) -> bool:
    """True when `name` is UNCONDITIONALLY not a firm identity: a known
    non-firm/placeholder title (Wix / domain-parking / template / spam / "for sale"),
    a URL, or nothing distinctive left after dropping generic words ("Law Firm",
    "Legal Services"). Practice-area / geographic DESCRIPTORS are handled separately by
    `is_descriptor_name` (kept by the caller when they match the firm's own domain), so
    a valid descriptive brand is never erroneously discarded here.
    """
    low = " ".join((name or "").lower().split())
    low_nodigit = re.sub(r"\s*\d+$", "", low)  # "mysite 1" -> "mysite"
    if low in _GENERIC_NAME or low_nodigit in _GENERIC_NAME or low in _NON_FIRM_NAMES:
        return True
    if any(
        s in low
        for s in (
            "template",
            "hugedomains",
            "godaddy",
            "for sale",
            "coming soon",
            "under construction",
        )
    ):
        return True  # site-builder / domain-parking / placeholder pages
    if re.search(r"\.(?:com|net|org|biz|info|law)\b", low):
        return True  # the candidate is a domain / URL, not a firm name
    return not firm_name_core(name or "")  # nothing distinctive left ("Law Firm")


def is_descriptor_name(name: str) -> bool:
    """A practice-area / geographic DESCRIPTOR rather than a firm identity:
    a single practice phrase ("Personal Injury Law Firm"), a multi-word list of
    practice areas ("Divorce Family Law"), or "{Geography} {practice area}". The caller
    KEEPS such a name when it echoes the firm's own domain (its chosen brand, e.g.
    "Carolina Family Law" on carolinafamilylaw.com) and drops it otherwise.
    """
    core = firm_name_core(name)
    if not core:
        return False
    tax = get_taxonomy()
    if tax.match(" ".join(core)) is not None:
        return True
    if len(core) >= 2 and all(tax.match(t) for t in core):
        return True
    # "{Geography} {practice}" — requires BOTH a geo token and a practice-area
    # remainder, so a bare place / surname ("Texas Law") is NOT flagged.
    non_geo = [t for t in core if t not in _GEO_TERMS]
    if non_geo and len(non_geo) < len(core):
        return tax.match(" ".join(non_geo)) is not None or all(tax.match(t) for t in non_geo)
    return False


def domain_consistent(name: str, host: str | None) -> bool:
    """A distinctive name token (>=4 chars) appears in the domain host. Real firms'
    domains usually echo their name (Fielding -> fieldinglawfirm.com), so this
    separates the real name from a co-occurring SEO descriptor."""
    if not host:
        return False
    stem = host.split(".")[0].replace("-", "")
    return any(len(t) >= 4 and t in stem for t in firm_name_core(name))


def has_entity_marker(name: str) -> bool:
    """True when the name carries an entity suffix / "&" — a strong real-firm signal."""
    low = (name or "").lower()
    padded = (f" {low} ", f" {low.replace('.', '')} ")  # match dotted "P.L.C." like "PLC"
    return any(mk in p for mk in _ENTITY_SUFFIX_MARK for p in padded)


def low_quality_reason(name: str | None, *, host: str | None = None) -> str | None:
    """Return a short reason string if `name` is a low-quality firm identity, else None.

    `host` (the firm's website host, when known) rescues a descriptive BRAND on its own
    domain. Reasons: ``"blank"``, ``"generic"`` (placeholder/URL/nothing distinctive),
    ``"descriptor"`` (practice/geo descriptor not tied to this firm). See the module
    docstring for the rename-not-drop policy.
    """
    if name is None or not " ".join(name.split()):
        return "blank"
    if is_generic_firm_name(name):
        return "generic"
    if is_descriptor_name(name):
        if (host and domain_consistent(name, host)) or has_entity_marker(name):
            return None  # rescued: the firm's own brand / a real entity name
        return "descriptor"
    return None


def is_low_quality_firm_name(name: str | None, *, host: str | None = None) -> bool:
    """True when `name` is not a usable firm identity (generic/placeholder/URL/
    denylisted, or an unrescued practice/geo descriptor). Pass `host` so a descriptive
    brand on its matching domain (e.g. "Carolina Family Law" on carolinafamilylaw.com)
    is correctly KEPT. A True result means "recover a better name and RENAME" — never,
    by itself, drop the firm.
    """
    return low_quality_reason(name, host=host) is not None
