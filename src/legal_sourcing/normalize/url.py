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

from urllib.parse import ParseResult, urlparse


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

    # Reject obviously bogus values.
    if not host or "." not in host:
        return None
    return host
