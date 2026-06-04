"""Shared identity helpers for resolution.

Two questions the matcher/fusion keep asking:

* **Is this website a firm identity?** A bare domain only identifies one specific
  firm if it is neither a directory/social/aggregator domain
  (``normalize.url.is_aggregator_domain``) nor a website-builder / DIY-host
  domain (``PLATFORM_DOMAINS`` below). ``weebly.com``, ``wixsite.com``,
  ``wordpress.com`` etc. show up as the *bare* normalized domain on a handful of
  records each; merging firms on such a shared value would fabricate a bogus
  mega-firm, exactly like the aggregator domains do. We keep the platform list
  here so resolution can be strict without editing the shared normalizer
  (ideally these fold into ``AGGREGATOR_DOMAINS`` later).

* **Does this name look like a firm (vs a person)?** ``normalize.name`` has a
  ``looks_like_firm`` but its short markers ``" pa"`` / ``" pc"`` substring-match
  common surnames ("**Pa**rker", "**Pa**trick", "**Pa**rks"), so person names
  wrongly pass the gate. ``is_firm_name`` below fixes that (treats ``pa``/``pc``
  only as standalone trailing entity-suffix tokens) while still satisfying the
  existing ``looks_like_firm`` test cases. The shared bug is flagged for the
  normalize owner.
"""

from __future__ import annotations

import re

from legal_sourcing.normalize.url import is_aggregator_domain

# Website-builder / DIY-host bare domains: a "website" that is just one of these
# is not a distinctive firm identity. Complements normalize.url.AGGREGATOR_DOMAINS.
PLATFORM_DOMAINS: frozenset[str] = frozenset(
    {
        "wix.com",
        "wixsite.com",
        "squarespace.com",
        "wordpress.com",
        "wordpress.org",
        "weebly.com",
        "godaddysites.com",
        "godaddy.com",
        "blogspot.com",
        "webs.com",
        "tripod.com",
        "webnode.com",
        "site123.com",
        "jimdo.com",
        "strikingly.com",
        "mystrikingly.com",
        "business.site",
    }
)


def _is_platform_domain(domain: str | None) -> bool:
    if not domain:
        return False
    d = domain.strip().lower().lstrip(".")
    if d.startswith("www."):
        d = d[4:]
    return any(d == p or d.endswith("." + p) for p in PLATFORM_DOMAINS)


def is_identity_website(domain: str | None) -> bool:
    """True when `domain` (a bare domain from `normalize_url`) identifies one
    specific firm -- i.e. not empty, not an aggregator/social/directory domain,
    and not a website-builder platform domain."""
    if not domain or not domain.strip():
        return False
    return not is_aggregator_domain(domain) and not _is_platform_domain(domain)


# Substring markers that flag a firm/organization name. NOTE: deliberately omits
# the bare " pa"/" pc" forms (they substring-match "Parker", "Patrick", ...);
# those are handled as standalone trailing entity-suffix tokens instead.
_FIRM_SUBSTR_MARKERS: tuple[str, ...] = (
    " llp",
    " lllp",
    " llc",
    " pllc",
    " plc",
    " ltd",
    " inc",
    " corp",
    "law ",
    " law",
    "firm",
    "group",
    "associates",
    "attorney",
    "lawyer",
    "offices",
    "counsel",
    "partners",
    "legal",
    " & ",
)
# Entity suffixes accepted only as a STANDALONE trailing token.
_FIRM_SUFFIX_TOKENS: frozenset[str] = frozenset(
    {
        "llp",
        "lllp",
        "llc",
        "pllc",
        "plc",
        "pc",
        "pa",
        "ltd",
        "inc",
        "corp",
        "co",
        "chtd",
        "chartered",
    }
)


def is_firm_name(name: str | None) -> bool:
    """Heuristic: does `name` look like a firm/organization (vs a person)?

    Corrected variant of ``normalize.name.looks_like_firm`` -- still True for
    firm markers ("law", "& ", "group", "LLP", ...) but no longer fooled by
    surnames that merely start with "pa"/"pc".
    """
    if not name:
        return False
    low = f" {name.strip().lower()} "
    if any(m in low for m in _FIRM_SUBSTR_MARKERS):
        return True
    tokens = re.findall(r"[a-z.&]+", name.lower())
    if tokens:
        last = tokens[-1].replace(".", "")
        if last in _FIRM_SUFFIX_TOKENS:
            return True
    return False
