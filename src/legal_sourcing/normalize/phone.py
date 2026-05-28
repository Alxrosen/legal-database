"""Phone normalization to E.164 via the `phonenumbers` library."""

from __future__ import annotations

import phonenumbers


def normalize_phone(raw: str | None, *, default_region: str = "US") -> str | None:
    """Return E.164 form (e.g. '+16025551234') or None if unparseable.

    Accepts None / empty / whitespace as None. Does NOT raise on bad
    input — callers should check for None.
    """
    if not raw or not str(raw).strip():
        return None
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(parsed):
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
