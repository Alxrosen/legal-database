"""URL canonicalization for matching firm websites.

Goal: collapse "https://www.Example.com/path/?utm=x" and
"http://example.com" to the same comparison key — the bare apex domain.

We preserve only the registered domain (eTLD+1 is too eager — "co.uk"
TLDs would over-strip; we use a simple "drop the www. and any path/query/
fragment" approach). For most law-firm sites this is enough; if a firm
runs `www.smith.lawyer` vs `smith.lawyer`, both normalize to
`smith.lawyer`.
"""

from __future__ import annotations

from urllib.parse import ParseResult, urljoin, urlparse


def safe_urlparse(url: str) -> ParseResult | None:
    """urlparse that never raises. Python 3.14 made urlparse raise
    ValueError on malformed URLs (stray brackets, bad IPv6 literals);
    scraped hrefs contain plenty of junk, so callers want a None
    rather than a crash that aborts a whole parse/normalize pass.
    """
    try:
        return urlparse(url)
    except ValueError:
        return None


def safe_urljoin(base: str, url: str) -> str | None:
    """urljoin that never raises. Python 3.14's urljoin internally re-parses
    both arguments and raises ValueError on malformed input (stray brackets /
    bad IPv6 literals) — and that happens BEFORE any safe_urlparse wrapper runs.
    Scraped hrefs contain plenty of junk, so callers want None rather than a
    crash that aborts a whole parse pass. Mirror of safe_urlparse.
    """
    try:
        return urljoin(base, url)
    except ValueError:
        return None


def strip_self_domain(url: str | None, own_domains: tuple[str, ...]) -> str | None:
    """Drop a website URL that points at the directory's OWN domain.

    A source must never record itself as a firm's website: because
    `normalize_url` collapses to the bare domain, every firm that leaked
    e.g. `findlaw.com` would share one website key and create false
    website matches across unrelated firms in the resolution layer.

    Returns None if `url`'s host equals or is a subdomain of any entry in
    `own_domains` (e.g. `findlaw.com` also catches `lawyers.findlaw.com`).
    Otherwise returns `url` unchanged. Unparseable input returns None.
    """
    if not url:
        return None
    host = normalize_url(url)
    if host is None:
        return None
    for d in own_domains:
        d = d.strip().lower().lstrip(".")
        if d and (host == d or host.endswith("." + d)):
            return None
    return url


# Third-party legal directories, lead-gen aggregators, social, and maps
# domains. A firm "website" pointing here is not a distinctive firm
# domain; because normalize_url collapses to the bare domain, such a
# value would be SHARED across unrelated firms and fabricate website
# matches in resolution. Treated as "no website" for matching purposes.
AGGREGATOR_DOMAINS: frozenset[str] = frozenset(
    {
        # legal directories / lead-gen
        "lawfirms.com",
        "avvo.com",
        "lawyers.com",
        "martindale.com",
        "findlaw.com",
        "justia.com",
        "justia.lawyer",
        "nolo.com",
        "superlawyers.com",
        "expertise.com",
        "lawinfo.com",
        "legalmatch.com",
        "attorneys.com",
        "hg.org",
        "thervo.com",
        # social / maps / generic
        "facebook.com",
        "linkedin.com",
        "twitter.com",
        "x.com",
        "instagram.com",
        "youtube.com",
        "google.com",
        "business.google.com",
        "g.page",
        "goo.gl",
        "bing.com",
        "yelp.com",
        # Bar associations / official member-directories of record — never a single
        # firm's own website (azbar.org was mis-attributed as "WSChick PC"'s site).
        # The GENERAL catch is the extractor's firm-name<->domain identity match +
        # the .gov/.edu rule (covers e.g. calbar.ca.gov); these are the frequent
        # non-.gov bar hosts worth hard-listing.
        "americanbar.org",
        "abanet.org",
        "azbar.org",
        "floridabar.org",
        "texasbar.com",
        "nysba.org",
        "wsba.org",
        "illinoisbar.org",
        "michbar.org",
        "ncbar.org",
        "missouribar.com",
        "ohiobar.org",
    }
)


def is_aggregator_domain(host: str | None) -> bool:
    """True if `host` (a bare domain from `normalize_url`) is a known
    directory / aggregator / social domain that must not be used as a
    firm-website match key. Matches the domain and any subdomain."""
    if not host:
        return False
    h = host.strip().lower().lstrip(".")
    if h.startswith("www."):
        h = h[4:]
    return any(h == d or h.endswith("." + d) for d in AGGREGATOR_DOMAINS)


def normalize_url(raw: str | None) -> str | None:
    """Return bare-domain form of a URL/host string, or None if not
    parseable.

    Examples:
        >>> normalize_url("https://www.Example.com/path/")
        'example.com'
        >>> normalize_url("http://example.com")
        'example.com'
        >>> normalize_url("example.com")
        'example.com'
        >>> normalize_url("www.smith-jones.lawyer/contact")
        'smith-jones.lawyer'
    """
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip().lower()

    # Make sure urlparse picks up the netloc even when scheme is missing.
    # Already protocol-relative ("//host/...") is fine as-is; bare hosts
    # need the leading "//".
    if "://" not in s and not s.startswith("//"):
        s = "//" + s

    # Python 3.14 made urlparse strict: malformed URLs (e.g. a stray
    # "[" that looks like a broken IPv6 literal) now raise ValueError
    # instead of best-effort parsing. Source data has plenty of junk
    # URLs, so swallow the error and treat the value as unparseable.
    try:
        parsed = urlparse(s)
    except ValueError:
        return None
    host = parsed.netloc or parsed.path  # bare strings end up in `path`
    host = host.split("/")[0]  # in case path leaked in via no-scheme input
    host = host.split("?")[0].split("#")[0]
    host = host.strip(".")

    if host.startswith("www."):
        host = host[4:]

    # Reject email addresses mistakenly stored as websites — AZ Bar's
    # FirmURL field sometimes holds an email (e.g. "name@yahoo.com"),
    # which would otherwise normalize to a shared domain and pollute the
    # website match key. The "@" (userinfo) marks it as not a firm site.
    if "@" in host:
        return None

    # Reject obviously bogus values.
    if not host or "." not in host:
        return None
    return host
