"""Phone normalization tests."""

from __future__ import annotations

import pytest

from legal_sourcing.normalize.phone import normalize_phone


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("(602) 555-1234", "+16025551234"),
        ("602-555-1234", "+16025551234"),
        ("602.555.1234", "+16025551234"),
        ("6025551234", "+16025551234"),
        ("+1 602 555 1234", "+16025551234"),
        ("1-602-555-1234", "+16025551234"),
        # Extension is dropped — E.164 strict format does not include it.
        # If we later need the extension we'll switch to RFC3966 format.
        ("(602) 555-1234 ext. 5", "+16025551234"),
        # Invalid / unparseable
        ("not a phone", None),
        ("", None),
        (None, None),
        ("   ", None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_non_us_default_region():
    # UK number, parsed with GB default region.
    assert normalize_phone("020 7946 0958", default_region="GB") == "+442079460958"
