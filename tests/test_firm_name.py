"""Tests for the shared firm-name quality predicate (`normalize/firm_name.py`).

Pins the behaviour the three consumers (website extraction, Fixer cleanup, Canonizer
floor) rely on. Includes the cross-source cleanup's full 36-case adversarial backstop
(digit/initials brands, URL-form brands, own-domain echo rescues) folded into the
shared util on 2026-06-12.
"""

import pytest

from legal_sourcing.normalize.firm_name import (
    NAME_STOPWORDS,
    firm_name_core,
    host_echoes,
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
        "HugeDomains.com",  # parking — denylisted, never rescued by its stem
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
        "BrentCorwin.com",  # URL-form with a distinctive stem IS an identity
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


# --- The cross-source cleanup's adversarial backstop (fix_generic_names.py), now the
# --- contract of the shared util: (name, host, expected low_quality_reason).
BACKSTOP_CASES = [
    # unambiguous junk -> flagged
    ("Attorney at Law", None, "generic"),
    ("law office", None, "generic"),
    ("Law", None, "generic"),
    ("Alabama Personal Injury Law Firm", None, "descriptor"),
    ("Florida Injury Law Group", None, "descriptor"),
    # bare entity suffix is NOT identity -> still flagged
    ("A Law Firm, P.C.", None, "generic"),
    # descriptor on its OWN domain -> brand, kept
    ("Nevada Family Law Group", "nevadafamilylaw.com", None),
    # ampersand names -> kept (multi-party structure)
    ("J&Y Law", "jnylaw.com", None),
    ("F&B Law Firm, P.C.", None, None),
    ("M&H Legal Services, LLC", None, None),
    ("Treon & Shook, PLLC", None, None),
    ("Morgan & Morgan", None, None),
    # digit brands -> kept
    ("The 928 Law Firm", "928law.com", None),
    ("D2 Injury Law", "denmonpearlman.com", None),
    ("5280 Law Group", None, None),
    ("805 Law Group", None, None),
    ("615 Lawyer", None, None),
    ("THE702FIRM Injury Attorneys", "the702firm.com", None),
    # initials brands (incl. dotted) -> kept
    ("The H Law Group", "thehfirm.com", None),
    ("The W Law Firm", None, None),
    ("J.K. Lawyers", "jklawyers.com", None),
    ("S.M.F. Law", None, None),
    ("M.C. Law Group", None, None),
    ("A.R.K., Inc.", None, None),
    ("V.E.M. Attorney at Law", None, None),
    # URL-form names with a distinctive stem -> kept
    ("Otto.Law", "otto.law", None),
    ("BrentCorwin.com", None, None),
    ("Chewy.com", None, None),
    ("iTicket.Law - Powered by Hatley Law Office", "iticket.law", None),
    # own-domain echo: prefix / containment / acronym -> kept
    ("Best Law Firm", "bestlawaz.com", None),
    ("MAS Law", "mas.law", None),
    ("Business Law Center", "blc-plc.com", None),
    ("The Estate Planning Law Group", "teplg.com", None),
    ("Trusted Estate Planning Attorneys", "trustedepa.com", None),
    # ...but the SAME names with no domain to anchor them stay flagged
    ("Best Law Firm", None, "generic"),
    ("Business Law Center", None, "descriptor"),
]


@pytest.mark.parametrize(("name", "host", "expected"), BACKSTOP_CASES)
def test_cleanup_backstop(name, host, expected):
    assert low_quality_reason(name, host=host) == expected


def test_firm_name_core_counts_digit_and_initials_tokens():
    assert firm_name_core("The 928 Law Firm") == ["928"]
    assert firm_name_core("J.K. Lawyers") == ["jk"]
    assert firm_name_core("The H Law Group") == ["h"]
    assert firm_name_core("A Law Firm, P.C.") == []  # filler "a" + suffixes only


def test_host_echoes_variants():
    assert host_echoes("MAS Law", "mas.law") is True  # containment
    assert host_echoes("Best Law Firm", "bestlawaz.com") is True  # shared prefix
    assert host_echoes("Business Law Center", "blc-plc.com") is True  # acronym
    assert host_echoes("Best Law Firm", "acmelegal.com") is False
    assert host_echoes("Best Law Firm", None) is False


def test_stopwords_exported():
    assert "llp" in NAME_STOPWORDS and "the" in NAME_STOPWORDS
