"""Tests for the shared firm-name quality predicate (`normalize/firm_name.py`).

Pins the behaviour the three consumers (website extraction, Fixer cleanup, Canonizer
floor) rely on, including the host-based descriptor rescue and the short-real-name
false-positive guard Fixer flagged.
"""

import pytest

from legal_sourcing.normalize.firm_name import (
    is_low_quality_firm_name,
    low_quality_reason,
)


@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        "   ",
        "Law Firm",
        "Legal Services",
        "Law Office",
        "Home",
        "Welcome",
        "Our Team",
        "Attorneys",
        "New Page",
        "mysite 1",
        "HugeDomains.com",
        "bestlawyer.net",  # bare domain / URL
        "poring168",  # frequency-flagged spam
        "School of Law",  # non-firm legal entity
    ],
)
def test_low_quality_flagged(name):
    assert is_low_quality_firm_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "Personal Injury Law Firm",  # pure practice-area descriptor
        "Florida Personal Injury Law Firm",  # geo + practice-area descriptor
    ],
)
def test_descriptor_flagged_without_host(name):
    assert is_low_quality_firm_name(name) is True
    assert low_quality_reason(name) == "descriptor"


@pytest.mark.parametrize(
    "name",
    [
        "Charles F. Myers, P.A.",  # entity suffix
        "Treon & Shook, PLLC",
        "Jones Walker LLP",
        "Smith & Associates",
        "Kean Miller",  # short, no suffix, but a real distinctive surname pair
        "Bryan Cave Leighton Paisner",
    ],
)
def test_real_names_kept(name):
    assert is_low_quality_firm_name(name) is False
    assert low_quality_reason(name) is None


def test_descriptor_rescued_by_domain_echo():
    # A practice descriptor that IS the firm's own brand on its matching domain is kept.
    name = "Personal Injury Law Firm"
    assert is_low_quality_firm_name(name) is True  # no host -> flagged
    assert is_low_quality_firm_name(name, host="personalinjurylawfirm.com") is False


def test_descriptor_not_rescued_by_unrelated_domain():
    # Same descriptor on an unrelated domain stays flagged (generic SEO, not a brand).
    assert is_low_quality_firm_name("Personal Injury Law Firm", host="acmelegal.com") is True


def test_entity_suffix_overrides_descriptor():
    # An entity marker rescues even a descriptor-shaped name (real registered entity).
    assert is_low_quality_firm_name("Personal Injury Law Firm, PLLC") is False
