"""URL normalization tests."""

from __future__ import annotations

import pytest

from legal_sourcing.normalize.url import normalize_url


@pytest.mark.parametrize(
    "raw",
    [
        # Python 3.14 urlparse raises ValueError on these; normalize_url
        # must swallow it and return None rather than crash the whole
        # normalize pass. This is the bug that killed the AZ Bar full
        # sweep mid-run (35,864 fetched, 0 stored).
        "http://[malformed",
        "https://exa[mple.com",
        "[::bad",
        "www.foo].com",
        "http://]",
    ],
)
def test_normalize_url_never_raises_on_malformed(raw):
    # Must not raise; returns None for unparseable junk.
    assert normalize_url(raw) is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://www.Example.com/path/", "example.com"),
        ("http://example.com", "example.com"),
        ("example.com", "example.com"),
        ("WWW.EXAMPLE.COM", "example.com"),
        ("https://example.com:8080/contact?utm=x#section", "example.com:8080"),
        ("www.smith-jones.lawyer/contact", "smith-jones.lawyer"),
        ("//cdn.example.com/foo", "cdn.example.com"),
        # Bare host with subdomain stays — we only strip "www."
        ("https://blog.example.com/", "blog.example.com"),
        # Garbage / empty
        ("", None),
        (None, None),
        ("   ", None),
        ("not a url", None),  # no dot, gets rejected
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # AZ Bar FirmURL sometimes holds an email, not a website. These
        # must not normalize to a shared domain (would pollute the
        # website match key). See assumptions 2026-06-02.
        "mcginnislawyer@yahoo.com",
        "richmadril@yahoo.com",
        "mailto:someone@firm.com",
        "info@smithlaw.com",
    ],
)
def test_normalize_url_rejects_emails(raw):
    assert normalize_url(raw) is None
