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


def test_street_token_order_is_stable_address_number_first():
    """Regression: _STREET_TAGS was a set, so usaddress tags iterated
    in non-deterministic order. The FindLaw pilot surfaced this when
    "445 Dexter Ave., Suite 4050" normalized to "ave 445 dexter suite
    4050" instead of "445 dexter ave suite 4050". Same firm at same
    address must collapse to the same normalized street.
    """
    result = normalize_address("445 Dexter Ave., Suite 4050, Montgomery, AL 36104")
    assert result is not None
    s = result.street_normalized or ""
    # AddressNumber should come BEFORE the street name, not after.
    idx_number = s.find("445")
    idx_dexter = s.find("dexter")
    assert idx_number >= 0 and idx_dexter >= 0
    assert idx_number < idx_dexter, f"street_normalized has wrong token order: {s!r}"


def test_street_token_order_stable_across_runs():
    """The same input must always produce the same output."""
    a = normalize_address("100 Main St, Suite 5, Phoenix, AZ 85001")
    b = normalize_address("100 Main St, Suite 5, Phoenix, AZ 85001")
    assert a is not None and b is not None
    assert a.street_normalized == b.street_normalized
