"""Shared text normalization primitives.

These are the building blocks every domain-specific normalizer composes:
nothing here is firm-specific or practice-area-specific. Each function
takes a string (or None) and returns a string (or None) — never mutates,
never raises on empty input.
"""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE_RE = re.compile(r"\s+")
# Strip every punctuation/symbol char, but keep alphanumerics and spaces.
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)


def collapse_whitespace(s: str) -> str:
    return _WHITESPACE_RE.sub(" ", s).strip()


def strip_diacritics(s: str) -> str:
    """Café -> Cafe. Useful before lowercasing for cross-source match."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def strip_punctuation(s: str) -> str:
    return _PUNCT_RE.sub(" ", s)


def basic_normalize(s: str | None) -> str | None:
    """Lowercase + strip diacritics + strip punctuation + collapse whitespace.

    Returns None for None / empty / whitespace-only input.
    """
    if s is None:
        return None
    s = strip_diacritics(s)
    s = s.lower()
    s = strip_punctuation(s)
    s = collapse_whitespace(s)
    return s or None


def strip_words(s: str, words: set[str]) -> str:
    """Remove whole-word occurrences of any token in `words`. Assumes `s`
    is already lowercased and punctuation-free (call after basic_normalize).
    """
    if not s:
        return s
    tokens = [t for t in s.split(" ") if t and t not in words]
    return " ".join(tokens)
