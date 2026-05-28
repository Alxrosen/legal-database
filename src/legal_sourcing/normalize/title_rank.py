"""Classify an attorney title into a seniority rank.

Higher rank = more senior. Returns None when the title is missing or
cannot be confidently classified. See docs/assumptions.md "Seniority
rule for primary contact" for the ladder and rationale.

Matching strategy: the classifier walks `_LADDER` top-to-bottom, first
whole-word phrase hit wins. Entries are ordered by SPECIFICITY (longest
or most distinctive phrases first), NOT by rank — otherwise "junior
associate" would be caught by the plain "associate" check and miscored.
"""

from __future__ import annotations

from legal_sourcing.normalize.text import basic_normalize, strip_words

# ORDER MATTERS — first matching phrase wins. Multi-word, more specific
# phrases must precede their single-word generic counterparts.
_LADDER: list[tuple[int, tuple[str, ...]]] = [
    # 100: very senior leadership
    (100, ("managing partner", "managing director", "chief executive")),
    # 90: founder / name-on-the-door
    (90, ("senior partner", "equity partner", "name partner", "founding partner", "founder")),
    # Specific-before-generic for associate / attorney / counsel:
    (30, ("junior associate", "staff attorney", "contract attorney", "law clerk")),
    (50, ("senior associate", "senior attorney")),
    (70, ("of counsel", "senior counsel")),
    # Generic single-word fallbacks (must come AFTER all multi-word
    # phrases that contain them):
    (80, ("partner", "principal", "shareholder", "member")),
    (60, ("counsel",)),
    (40, ("associate",)),
    (20, ("attorney", "lawyer")),
]

# Filler words stripped before keyword matching.
_TITLE_FILLERS: set[str] = {"the", "at", "for", "firm", "esq", "esquire"}


def classify_title(title: str | None) -> int | None:
    """Return seniority rank for `title`, or None if unclassifiable.

    Examples:
        "Managing Partner"  -> 100
        "Of Counsel"        -> 70
        "Senior Associate"  -> 50
        "Associate"         -> 40
        "Junior Associate"  -> 30
        "Attorney at Law"   -> 20
        ""                  -> None
        None                -> None
    """
    base = basic_normalize(title)
    if base is None:
        return None
    cleaned = strip_words(base, _TITLE_FILLERS).strip()
    if not cleaned:
        return None

    padded = f" {cleaned} "
    for rank, phrases in _LADDER:
        for phrase in phrases:
            if f" {phrase} " in padded:
                return rank
    return None
