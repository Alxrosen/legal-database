"""Address normalization tests.

usaddress's tagger is statistical, so we test for "what we need
downstream" rather than exhaustive ground truth.
"""

from __future__ import annotations

from legal_sourcing.normalize.address import normalize_address


def test_basic_arizona_address():
    result = normalize_address("123 Main St., Phoenix, AZ 85001")
    assert result is not None
    assert result.city == "Phoenix"
    assert result.state == "AZ"
    assert result.postal_code == "85001"
    assert result.street is not None
    assert "main" in result.street_normalized
    assert "123" in result.street_normalized


def test_address_with_suite():
    result = normalize_address("1 N Central Ave, Suite 2200, Phoenix, AZ 85004")
    assert result is not None
    assert result.state == "AZ"
    assert result.postal_code == "85004"
    # Suite should land in the street normalization (we fold OccupancyIdentifier in).
    assert result.street_normalized is not None
    assert "2200" in result.street_normalized


def test_zip_plus_four_truncated():
    result = normalize_address("1 N Central Ave, Phoenix, AZ 85004-1234")
    assert result is not None
    assert result.postal_code == "85004"


def test_empty_input_returns_none():
    assert normalize_address("") is None
    assert normalize_address(None) is None
    assert normalize_address("   ") is None
