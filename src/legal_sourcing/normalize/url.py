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

from urllib.parse import urlparse


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

    parsed = urlparse(s)
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
