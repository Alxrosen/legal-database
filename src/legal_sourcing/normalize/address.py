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

from dataclasses import dataclass

import usaddress

from legal_sourcing.normalize.text import basic_normalize

# Mapping from usaddress tag names to our normalized component fields.
_STREET_TAGS = {
    "AddressNumber",
    "AddressNumberPrefix",
    "AddressNumberSuffix",
    "StreetNamePreDirectional",
    "StreetNamePreModifier",
    "StreetNamePreType",
    "StreetName",
    "StreetNamePostType",
    "StreetNamePostDirectional",
    "StreetNamePostModifier",
}
_UNIT_TAGS = {"OccupancyType", "OccupancyIdentifier"}


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
        except Exception:  # noqa: BLE001
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

    street_tokens = [parsed[k] for k in _STREET_TAGS if k in parsed]
    unit_tokens = [parsed[k] for k in _UNIT_TAGS if k in parsed]
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
