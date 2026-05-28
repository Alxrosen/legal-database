"""Practice-area normalization + matching against the YAML taxonomy.

Policy (locked, see docs/assumptions.md "Practice-area matching is
exact-on-normalized"):

  1. Normalize both source-supplied strings AND every alias from the
     YAML the same way.
  2. Match by EXACT equality on the normalized string. No fuzzy.
  3. Unmatched strings are returned separately so the caller can log
     them to `unmatched_practice_areas`.

Normalization steps:

  * basic_normalize: lowercase + strip diacritics + strip punctuation +
    collapse whitespace.
  * strip filler words: law, attorney, attorneys, lawyer, lawyers,
    legal, services, practice.
  * singularize via `inflect`.

The YAML loader + DB upsert is here too — same module owns both the
in-memory and the persisted projection so they cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import inflect
import yaml

from legal_sourcing.normalize.text import basic_normalize, strip_words

# Filler words stripped before matching. Order-independent.
_FILLER_WORDS: set[str] = {
    "law",
    "attorney",
    "attorneys",
    "lawyer",
    "lawyers",
    "legal",
    "services",
    "practice",
}

_inflect = inflect.engine()


def _singularize_token(tok: str) -> str:
    # `inflect.singular_noun` returns False if the token is already
    # singular; in that case we keep the original token.
    singular = _inflect.singular_noun(tok)
    return singular if isinstance(singular, str) and singular else tok


def normalize_practice_area(raw: str | None) -> str | None:
    """Apply the full normalization pipeline. Returns None for empty
    input or for strings that normalize to empty (e.g. "law" alone).
    """
    base = basic_normalize(raw)
    if base is None:
        return None
    stripped = strip_words(base, _FILLER_WORDS)
    if not stripped:
        return None
    singularized = " ".join(_singularize_token(t) for t in stripped.split(" "))
    singularized = singularized.strip()
    return singularized or None


# ----------------------------------------------------------------------
# YAML loading and matching


@dataclass(frozen=True)
class TaxonomyArea:
    slug: str
    name: str
    parent_slug: str | None
    priority: bool
    description: str | None
    aliases: tuple[str, ...]  # raw alias strings from YAML


@dataclass
class Taxonomy:
    """In-memory view of the practice-area taxonomy.

    `alias_index` maps normalized alias string -> canonical slug. The
    canonical area's own name is also indexed as an alias of itself.
    """

    areas: dict[str, TaxonomyArea]
    alias_index: dict[str, str]

    def match(self, raw: str) -> str | None:
        """Return canonical slug or None."""
        key = normalize_practice_area(raw)
        if key is None:
            return None
        return self.alias_index.get(key)

    def rollup_chain(self, slug: str) -> list[str]:
        """Return [slug, parent, grandparent, ...] up to root."""
        chain: list[str] = []
        cur: str | None = slug
        while cur is not None and cur in self.areas:
            chain.append(cur)
            cur = self.areas[cur].parent_slug
        return chain


def load_taxonomy(path: Path) -> Taxonomy:
    """Load and validate the practice-area YAML."""
    with open(path, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)

    raw_areas = data.get("areas", [])
    areas: dict[str, TaxonomyArea] = {}
    alias_index: dict[str, str] = {}

    for entry in raw_areas:
        slug = entry["slug"]
        if slug in areas:
            raise ValueError(f"Duplicate practice-area slug in YAML: {slug}")
        area = TaxonomyArea(
            slug=slug,
            name=entry["name"],
            parent_slug=entry.get("parent"),
            priority=bool(entry.get("priority", False)),
            description=entry.get("description"),
            aliases=tuple(entry.get("aliases", [])),
        )
        areas[slug] = area

        # Index the canonical name AND each alias under its normalized form.
        for alias_str in (area.name, *area.aliases):
            normalized = normalize_practice_area(alias_str)
            if normalized is None:
                continue
            if normalized in alias_index and alias_index[normalized] != slug:
                # Two different canonical areas claim the same alias.
                # This is a YAML bug and must fail loud.
                raise ValueError(
                    f"Alias collision: '{normalized}' is claimed by both "
                    f"'{alias_index[normalized]}' and '{slug}'"
                )
            alias_index[normalized] = slug

    # Validate parent references.
    for area in areas.values():
        if area.parent_slug and area.parent_slug not in areas:
            raise ValueError(
                f"Area '{area.slug}' references unknown parent '{area.parent_slug}'"
            )

    return Taxonomy(areas=areas, alias_index=alias_index)


@lru_cache(maxsize=1)
def get_taxonomy() -> Taxonomy:
    """Cached taxonomy load from the default location."""
    root = Path(__file__).resolve().parents[3]
    return load_taxonomy(root / "data" / "reference" / "practice_areas.yaml")
