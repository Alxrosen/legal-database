"""Back-stop for the Martindale office-address repair.

Martindale city-listing cards crammed the whole address into one location
field, so ``parse_full_location`` must recover ``[street, ]city, ST [zip]``
from a single string. These cases are the regression net for that parser
(mirrors ``scripts/fix_martindale_offices.py --backstop``). Add a row when a
new bad shape turns up in the corpus.
"""

from __future__ import annotations

import pytest

from legal_sourcing.normalize.address import parse_full_location

# (raw, expected city, expected state, expected postal_code)
CASES = [
    # street, city, ST, zip
    ("101 Court Sq Ste I, Abbeville, AL 36310-2135", "Abbeville", "AL", "36310"),
    ("36 Tanner St., Ste. 300, Haddonfield, NJ 08033", "Haddonfield", "NJ", "08033"),
    # city, ST, zip (no street)
    ("Abbeville, AL 36310-0608", "Abbeville", "AL", "36310"),
    ("New York, NY 10016", "New York", "NY", "10016"),
    # P.O. box as the leading "street"
    ("P.O. Box 610, Abbeville, AL 36310", "Abbeville", "AL", "36310"),
    # multi-word cities that usaddress truncates to the last token
    ("La Quinta, CA 92248-5969", "La Quinta", "CA", "92248"),
    ("Palos Verdes Peninsula, CA 90274-9570", "Palos Verdes Peninsula", "CA", "90274"),
    ("Half Moon Bay, CA 94019", "Half Moon Bay", "CA", "94019"),
    # hyphenated cities
    ("Cardiff-By-The-Sea, CA 92007", "Cardiff-By-The-Sea", "CA", "92007"),
    ("Winston-Salem, NC 27101", "Winston-Salem", "NC", "27101"),
    # malformed 9-digit (no hyphen) ZIP -> first 5
    ("2501 South Broadway, Little Rock, AR 722060000", "Little Rock", "AR", "72206"),
    # tower + suite
    ("1 World Trade Center, Suite 8500, New York, NY 10007", "New York", "NY", "10007"),
    # no ZIP
    ("Tuscaloosa, AL", "Tuscaloosa", "AL", None),
    # malformed-ZIP rows the deterministic tail can't anchor; usaddress recovers
    # the state, and the standalone-token guard keeps the city intact.
    ("P.O. Box 383, Pismo Beach, CA 9348", "Pismo Beach", "CA", "9348"),
    ("999 Corporate Dr., Ste. 260, Ladera Ranch, CA 92694  ​", "Ladera Ranch", "CA", "92694"),
]


@pytest.mark.parametrize("raw,city,state,zipc", CASES)
def test_parse_full_location(raw: str, city: str, state: str | None, zipc: str | None) -> None:
    na = parse_full_location(raw)
    assert na is not None
    assert na.city == city
    assert na.state == state
    assert na.postal_code == zipc


def test_does_not_fabricate_us_state() -> None:
    # "New York, New York" has no USPS state code; usaddress would hallucinate
    # "NE" from "New" — the standalone-token guard must reject that.
    na = parse_full_location("New York, New York")
    assert na is not None
    assert na.city == "New York"
    assert na.state is None


def test_foreign_address_keeps_state_none() -> None:
    na = parse_full_location("Cape Town, South Africa 7935")
    assert na is not None
    assert na.state is None


def test_empty_input_returns_none() -> None:
    assert parse_full_location("") is None
    assert parse_full_location(None) is None
    assert parse_full_location("   ") is None
