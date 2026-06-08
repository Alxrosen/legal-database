"""Address parsing + normalization via `usaddress`.

We return both the structured components (street, city, state,
postal_code) AND a `normalized` string suitable for blocking. The
structured components are the canonical truth; the normalized string is
the comparison form.

usaddress's tagger uses CRF on parsed tokens and is imperfect — we
catch RepeatedLabelError and fall back to a best-effort parse so we
never raise to the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import usaddress

from legal_sourcing.normalize.text import basic_normalize

# Mapping from usaddress tag names to our normalized component fields.
# Order matters: we join the matching tokens in this order to produce
# a stable normalized street string. A set here was a bug — iteration
# order was undefined, so the same raw address could collapse to
# different normalized strings across runs.
_STREET_TAG_ORDER: tuple[str, ...] = (
    "AddressNumberPrefix",
    "AddressNumber",
    "AddressNumberSuffix",
    "StreetNamePreDirectional",
    "StreetNamePreModifier",
    "StreetNamePreType",
    "StreetName",
    "StreetNamePostType",
    "StreetNamePostDirectional",
    "StreetNamePostModifier",
)
_UNIT_TAG_ORDER: tuple[str, ...] = ("OccupancyType", "OccupancyIdentifier")


@dataclass(frozen=True)
class NormalizedAddress:
    raw: str
    street: str | None
    street_normalized: str | None  # lowercased, punctuation-free street
    city: str | None
    state: str | None  # 2-letter
    postal_code: str | None  # 5-digit
    country: str | None  # "US" default


def normalize_address(raw: str | None) -> NormalizedAddress | None:
    """Best-effort parse of a free-text US address. Returns None for
    empty input. Never raises.
    """
    if not raw or not str(raw).strip():
        return None
    raw_str = str(raw).strip()

    try:
        parsed, _ = usaddress.tag(raw_str)
    except usaddress.RepeatedLabelError:
        # Best-effort: take the first labeling.
        try:
            parts = usaddress.parse(raw_str)
        except Exception:
            return NormalizedAddress(
                raw=raw_str,
                street=None,
                street_normalized=None,
                city=None,
                state=None,
                postal_code=None,
                country="US",
            )
        parsed = {}
        for value, label in parts:
            parsed.setdefault(label, []).append(value)
        # Collapse list values into space-joined strings.
        parsed = {k: " ".join(v) if isinstance(v, list) else v for k, v in parsed.items()}

    street_tokens = [parsed[k] for k in _STREET_TAG_ORDER if k in parsed]
    unit_tokens = [parsed[k] for k in _UNIT_TAG_ORDER if k in parsed]
    street = " ".join(street_tokens) if street_tokens else None
    if unit_tokens:
        street = (street or "") + " " + " ".join(unit_tokens)
        street = street.strip()

    city = parsed.get("PlaceName")
    state = parsed.get("StateName")
    if state:
        state = state.replace(".", "").strip().upper()[:2]
    zipcode = parsed.get("ZipCode")
    if zipcode:
        zipcode = zipcode.strip().split("-")[0][:5] or None

    return NormalizedAddress(
        raw=raw_str,
        street=street or None,
        street_normalized=basic_normalize(street) if street else None,
        city=city or None,
        state=state or None,
        postal_code=zipcode or None,
        country="US",
    )


# Valid USPS state / territory codes — used to anchor the location parse so a
# random 2-letter token (e.g. a street abbreviation) can't be mistaken for a
# state.
_US_STATES: frozenset[str] = frozenset(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
        "PR",
        "VI",
        "GU",
        "AS",
        "MP",
    ]
)
# A 5-digit ZIP, optionally followed by ZIP+4 (with or without the hyphen — some
# Martindale rows carry a malformed 9-digit run like "722060000").
_LOC_ZIP_RE = re.compile(r"(?P<zip>\d{5})(?:-?\d{4})?\s*$")
# A trailing 2-letter state token, after a comma or whitespace.
_LOC_STATE_RE = re.compile(r"[,\s]+(?P<state>[A-Za-z]{2})$")


def parse_full_location(raw: str | None) -> NormalizedAddress | None:
    """Parse a location string that crammed a whole address into one field.

    Martindale's city-listing cards put the full office address — e.g.
    ``"101 Court Sq Ste I, Abbeville, AL 36310-2135"`` or ``"Abbeville, AL
    36310"`` — into a single location element. The city parser's strict
    ``"City, ST"`` regex never matched these, so the whole string fell into
    ``city_raw`` with no state, blocking ``primary_state``.

    This recovers the structure with a deterministic, tail-anchored parse (the
    corpus is highly regular: ``[street, ]city, ST [zip]``). The state is the
    reliable anchor — a validated 2-letter USPS code before the ZIP — so we peel
    ZIP then state off the end, take the last remaining comma-segment as the
    city (which preserves multi-word names like ``"Palos Verdes Peninsula"``
    that ``usaddress`` truncates), and treat the remainder as the street. We
    fall back to ``usaddress`` only when no US-state anchor is found. Returns
    ``None`` for empty input; never raises. A genuinely foreign address (no US
    state) yields ``state=None`` rather than a fabricated one.
    """
    if not raw or not str(raw).strip():
        return None
    original = str(raw).strip()
    s = original

    zip5: str | None = None
    m = _LOC_ZIP_RE.search(s)
    if m:
        zip5 = m.group("zip")
        s = s[: m.start()].strip().rstrip(",").strip()

    state: str | None = None
    m2 = _LOC_STATE_RE.search(s)
    if m2 and m2.group("state").upper() in _US_STATES:
        state = m2.group("state").upper()
        s = s[: m2.start()].strip().rstrip(",").strip()

    parts = [p.strip() for p in s.split(",") if p.strip()]
    city = parts[-1] if parts else None
    street = ", ".join(parts[:-1]) if len(parts) > 1 else None

    if state is None:
        # No clean state tail — usually a malformed ZIP ("92618 I", a 6-digit
        # run, trailing junk) that broke the anchor. usaddress is more tolerant,
        # so borrow ITS state — but only if that 2-letter code actually appears
        # as a standalone token in the input. That guard is load-bearing: it
        # stops usaddress hallucinating e.g. "NE" out of "New York". We keep our
        # own city/street split, which preserves multi-word city names that
        # usaddress truncates ("Pismo Beach" -> "Beach").
        na = normalize_address(original)
        if (
            na
            and na.state in _US_STATES
            and re.search(rf"(?<![A-Za-z]){na.state}(?![A-Za-z])", original, re.IGNORECASE)
        ):
            state = na.state
            head = (
                re.split(
                    rf"(?<![A-Za-z]){state}(?![A-Za-z])",
                    original,
                    maxsplit=1,
                    flags=re.IGNORECASE,
                )[0]
                .strip()
                .rstrip(",")
                .strip()
            )
            hparts = [p.strip() for p in head.split(",") if p.strip()]
            city = hparts[-1] if hparts else None
            street = ", ".join(hparts[:-1]) if len(hparts) > 1 else None
            zip5 = na.postal_code or zip5

    return NormalizedAddress(
        raw=original,
        street=street or None,
        street_normalized=basic_normalize(street) if street else None,
        city=city.title() if city else None,
        state=state,
        postal_code=zip5,
        country="US",
    )
