"""Firm-name normalization tests.

Key cases:
  * Entity suffix stripping: LLP / PC / P.A. all peel off.
  * "Associates" / "Group" / "Law" are NOT stripped here — they're part
    of the name. (Filler-word stripping is a practice-area-only thing.)
  * "Bow Street LLP" and "Bow Street Associates" must produce DIFFERENT
    normalized forms — they're distinct firms.
  * Suffix difference is preserved on the returned dataclass so callers
    can use it as a small negative signal during matching.
"""

from __future__ import annotations

import pytest

from legal_sourcing.normalize.name import normalize_firm_name


@pytest.mark.parametrize(
    "raw, normalized, suffix",
    [
        # Standard entity suffixes
        ("Bow Street, LLP", "bow street", "llp"),
        ("Bow Street LLP", "bow street", "llp"),
        ("Bow Street, P.A.", "bow street", "p a"),
        ("Bow Street P.C.", "bow street", "p c"),
        ("Bow Street, PC", "bow street", "pc"),
        ("Acme Law, LLC", "acme law", "llc"),
        ("Acme PLLC", "acme", "pllc"),
        ("Smith Inc.", "smith", "inc"),
        ("Smith & Jones, P.A.", "smith jones", "p a"),
        # Mixed case + punctuation + diacritics
        ("CAFÉ & PARTNERS, LLP", "cafe partners", "llp"),
        # Multiple trailing tokens that aren't suffixes stay put
        ("Bow Street Associates", "bow street associates", None),
        ("Bow Street Group", "bow street group", None),
        ("Smith Law Group", "smith law group", None),
        # No suffix
        ("Smith", "smith", None),
        # Empty / None
        ("", None, None),
        (None, None, None),
    ],
)
def test_normalize_firm_name_examples(raw, normalized, suffix):
    result = normalize_firm_name(raw)
    if normalized is None:
        assert result is None or result.normalized == ""
    else:
        assert result is not None
        assert result.normalized == normalized
        assert result.suffix == suffix


def test_bow_street_llp_differs_from_associates():
    """Two distinct firms must produce distinct normalized forms."""
    llp = normalize_firm_name("Bow Street LLP")
    associates = normalize_firm_name("Bow Street Associates")
    assert llp is not None and associates is not None
    assert llp.normalized != associates.normalized
    # And the suffix signal is asymmetric — only one has an entity suffix.
    assert llp.suffix == "llp"
    assert associates.suffix is None


def test_double_suffix_extraction():
    """A name with a redundant suffix ("Smith LLP Inc") peels both off
    so the comparison form is the bare name. Suffix records the
    outermost one."""
    result = normalize_firm_name("Smith LLP, Inc.")
    assert result is not None
    assert result.normalized == "smith"
    assert result.suffix == "inc"  # outermost (rightmost) wins
