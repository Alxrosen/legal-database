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


# Slug -> USPS 2-letter abbreviation. Used to backfill a firm's office
# state when a source lists only the city on the card but the STATE is
# implied by the URL we scraped (e.g. Martindale /all-lawyers/{city}/{state}/).
STATE_SLUG_TO_ABBR: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new-hampshire": "NH",
    "new-jersey": "NJ",
    "new-mexico": "NM",
    "new-york": "NY",
    "north-carolina": "NC",
    "north-dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode-island": "RI",
    "south-carolina": "SC",
    "south-dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west-virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district-of-columbia": "DC",
}


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
