"""Firm-name normalization.

Goals:
  * Produce a stable comparison form that ignores capitalization,
    punctuation, diacritics, and entity suffixes (LLP, PC, Inc...) —
    these are not part of "the name."
  * Preserve substantive descriptors. "Bow Street Associates" must NOT
    normalize to "bow street" — "Associates" is part of how the firm is
    known and distinguishes it from "Bow Street LLP."
  * Return both the normalized form AND the extracted suffix, so
    resolution can use suffix difference as a small negative signal
    ("Acme LLP" vs "Acme PC" is *evidence* they may be related firms
    but distinct entities).

Out of scope here:
  * The "law firm" filler word stripping that practice-area normalization
    does — firm names like "Smith Law" should normalize to "smith law",
    not "smith". Filler-word stripping belongs in practice areas only.
"""

from __future__ import annotations

from dataclasses import dataclass

from legal_sourcing.normalize.text import basic_normalize, collapse_whitespace

# Business-entity suffixes. Tokens are matched as standalone words at
# the END of the normalized name. Order doesn't matter; matching is by
# set membership. Extend as we encounter new variants.
_ENTITY_SUFFIXES: set[str] = {
    "llp",
    "lllp",
    "llc",
    "pllc",
    "pc",
    "pa",
    "p a",  # "P.A." -> after strip_punctuation -> "p a"
    "p c",  # "P.C." -> "p c"
    "plc",
    "ltd",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "esq",
    "esquire",
    "chartered",
    "chtd",
}


@dataclass(frozen=True)
class NormalizedName:
    """Result of normalizing a firm name."""

    raw: str
    normalized: str  # full normalized form, suffix REMOVED
    suffix: str | None  # extracted entity suffix, if any (e.g. "llp")


def normalize_firm_name(name: str | None) -> NormalizedName | None:
    """Normalize a firm name.

    Examples:
        >>> normalize_firm_name("Bow Street, LLP").normalized
        'bow street'
        >>> normalize_firm_name("Bow Street, LLP").suffix
        'llp'
        >>> normalize_firm_name("Bow Street Associates").normalized
        'bow street associates'
        >>> normalize_firm_name("Smith & Jones, P.A.").normalized
        'smith jones'
        >>> normalize_firm_name("Smith & Jones, P.A.").suffix
        'p a'

    Returns None for None / empty / whitespace-only input.
    """
    if not name or not name.strip():
        return None

    base = basic_normalize(name)
    if base is None:
        return NormalizedName(raw=name, normalized="", suffix=None)

    # Walk tokens from the right, peeling off suffixes. Suffixes may be
    # one or two tokens ("p a" for "P.A."). We greedily match the
    # longest trailing suffix.
    tokens = base.split(" ")
    suffix: str | None = None
    while tokens:
        # Try 2-token then 1-token trailing match.
        if len(tokens) >= 2 and " ".join(tokens[-2:]) in _ENTITY_SUFFIXES:
            suffix = " ".join(tokens[-2:]) if suffix is None else suffix
            tokens = tokens[:-2]
            continue
        if tokens[-1] in _ENTITY_SUFFIXES:
            suffix = tokens[-1] if suffix is None else suffix
            tokens = tokens[:-1]
            continue
        break

    normalized = collapse_whitespace(" ".join(tokens))
    return NormalizedName(raw=name, normalized=normalized, suffix=suffix)


# Substrings that mark a string as a FIRM/organization name rather than a
# person's name. Used to tell a real firm from a person when a source field
# (e.g. a state-bar mailing-address line, or a FindLaw person-card) might hold
# either. Matched against the lower-cased name padded with spaces so " law "
# matches "Smith Law" but not "Lawson". Extend as new variants appear.
FIRM_NAME_MARKERS: tuple[str, ...] = (
    " llp",
    " lllp",
    " llc",
    " pllc",
    " pc",
    " p.c",
    " pa",
    " p.a",
    " plc",
    " ltd",
    " inc",
    " corp",
    "law ",
    " law",
    "firm",
    "group",
    "associates",
    "attorneys",
    "offices",
    " & ",
    "counsel",
    "partners",
)


def looks_like_firm(name: str | None) -> bool:
    """Heuristic: does `name` look like a firm/organization (vs a person)?

    True when any firm marker (entity suffix, "law", "& ", "group", etc.)
    appears in the padded, lower-cased name. Used by the canonical-apply
    attorney count and by sources whose firm field may actually hold a
    person's name (state bars, FindLaw person-cards).
    """
    if not name:
        return False
    low = f" {name.strip().lower()} "
    return any(m in low for m in FIRM_NAME_MARKERS)
