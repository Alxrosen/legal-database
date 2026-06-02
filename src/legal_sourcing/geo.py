"""US geography helpers.

Martindale and FindLaw both slug US states as the lowercase, hyphenated
full state name (``arizona``, ``new-york``, ``district-of-columbia``).
The national scrape iterates this universe; per-state city lists are
*discovered* at run time from each source's state index page rather than
hard-coded, because the two sources expose different city slugs.
"""

from __future__ import annotations

# 50 states + DC, in the slug form both sources use in their URLs.
US_STATE_SLUGS: tuple[str, ...] = (
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new-hampshire",
    "new-jersey",
    "new-mexico",
    "new-york",
    "north-carolina",
    "north-dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode-island",
    "south-carolina",
    "south-dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west-virginia",
    "wisconsin",
    "wyoming",
    "district-of-columbia",
)


def parse_states_arg(value: str | None) -> list[str]:
    """Turn a CLI ``--states`` value into a validated list of slugs.

    ``None`` or ``"all"`` -> the full national list. Otherwise a
    comma-separated list of slugs, each validated against
    ``US_STATE_SLUGS`` (raises ``ValueError`` on an unknown slug so a
    typo fails loud instead of silently scraping nothing).
    """
    if value is None or value.strip().lower() in ("", "all"):
        return list(US_STATE_SLUGS)
    requested = [s.strip().lower() for s in value.split(",") if s.strip()]
    known = set(US_STATE_SLUGS)
    unknown = [s for s in requested if s not in known]
    if unknown:
        raise ValueError(
            f"Unknown state slug(s): {unknown}. "
            f"Expected lowercase hyphenated names like 'arizona', 'new-york'."
        )
    return requested


__all__ = ["US_STATE_SLUGS", "parse_states_arg"]
