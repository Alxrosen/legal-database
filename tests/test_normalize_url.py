"""URL normalization tests."""

from __future__ import annotations

import pytest

from legal_sourcing.normalize.url import normalize_url


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
