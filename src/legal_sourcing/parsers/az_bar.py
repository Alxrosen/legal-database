"""AZ Bar parsers — list response and detail response.

Both parsers consume a JSON envelope from the api-proxy.azbar.org API
and emit firm-shaped dicts (one per attorney). Each emitted dict is a
"single-attorney firm" — the pipeline aggregates dicts with the same
(name_normalized, primary_office_street_normalized) into one
FirmSourceRecord with multiple contacts.

Normalization (the `*_normalized` columns) is NOT done here — it
happens in the pipeline layer per the project's "re-normalize without
re-parsing" rule.
"""

from __future__ import annotations

import json
from typing import Any

from legal_sourcing.parsers.base import BaseParser


# Company-field values that signal the attorney is not actively
# affiliated with a firm. Mapped to a canonical lowercase token.
_DEACTIVATION_MARKERS: dict[str, str] = {
    "retired": "retired",
    "inactive": "inactive",
    "deceased": "deceased",
    "deceased member": "deceased",
    "disbarred": "disbarred",
    "resigned": "resigned",
    "suspended": "suspended",
}

# Company values that mean "no firm" without implying deactivation.
_ABSENT_MARKERS: frozenset[str] = frozenset(
    {"n/a", "na", "none", "-", "--", "self", "self employed", "self-employed"}
)


def _flatten_areas_of_law(areas: Any) -> list[str]:
    """Flatten AZ Bar's nested ``AreasOfLawAndPractice`` into a flat
    list of category + sub-area strings.

    Detail-response shape::

        {"Personal Injury": ["Auto Accidents", "Med Mal", ...], ...}

    Returns top-level keys AND sub-area strings, both in source order.
    The canonical taxonomy matcher will dedupe by normalized form.
    """
    if not areas:
        return []
    flat: list[str] = []
    if isinstance(areas, dict):
        for top, subs in areas.items():
            if top:
                flat.append(str(top))
            if isinstance(subs, list):
                for s in subs:
                    if s:
                        flat.append(str(s))
    elif isinstance(areas, list):
        # Defensive: future shape change might make this a flat list.
        flat.extend(str(x) for x in areas if x)
    return flat


def _office_dict(addr: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert the AZ Bar ``Address`` subobject to an office dict
    matching the documented FirmSourceRecord.offices entry shape (raw
    fields only)."""
    if not addr:
        return None
    # Empty-address case: AZ Bar emits {} or all-empty strings for
    # attorneys without a public address. Don't synthesize a row.
    fields = {
        "street_raw": (addr.get("Address1") or "").strip() or None,
        "street2_raw": (addr.get("Address2") or "").strip() or None,
        "city_raw": (addr.get("City") or "").strip() or None,
        "state_raw": (addr.get("State") or "").strip() or None,
        "postal_code_raw": (addr.get("Zip") or "").strip() or None,
        "county_raw": (addr.get("County") or "").strip() or None,
    }
    if not any(fields.values()):
        return None
    return {
        "label": None,
        "is_primary": True,
        "country_raw": "US",
        **fields,
    }


def _attorney_contact(record: dict[str, Any]) -> dict[str, Any]:
    """Build a contact entry from one attorney's AZ Bar record.

    Works for both list-summary records and detail records — detail
    simply has more keys populated. AZ Bar does not expose attorney
    title, so `title` is always None from this source; downstream
    title_rank classification will return None for these.
    """
    first = (record.get("FirstName") or "").strip() or None
    middle = (record.get("MiddleName") or "").strip() or None
    last = (record.get("LastName") or "").strip() or None
    full = " ".join(p for p in (first, middle, last) if p) or None

    phones_list = record.get("PhoneNumbers") or []
    primary_phone = (record.get("PrimaryPhone") or "").strip() or None
    if not primary_phone and phones_list:
        # Best effort — take the first non-empty entry's phone field if
        # it's a dict, else the value directly.
        first_phone = phones_list[0]
        if isinstance(first_phone, dict):
            primary_phone = (
                first_phone.get("PhoneNumber") or first_phone.get("Phone") or None
            )
        else:
            primary_phone = str(first_phone) or None

    contact: dict[str, Any] = {
        "name_raw": full,
        "first_name": first,
        "middle_name": middle,
        "last_name": last,
        "title": None,  # AZ Bar does not expose attorney title
        "email_raw": (record.get("Email") or "").strip() or None,
        "phone_raw": primary_phone,
        "bar_number": (record.get("BarNumber") or "").strip() or None,
        "bar_state": "AZ",
        # Source-specific extras (preserved as part of the contact JSON):
        "entity_number": record.get("EntityNumber"),
        "member_status": record.get("MemberStatus"),
    }

    # Detail-only fields when present. Skip keys with empty values.
    detail_only_keys = (
        ("name_prefix", "NamePrefix"),
        ("name_suffix", "NameSuffix"),
        ("admitted_year", "AdmittedYear"),
        ("az_admit_date", "AzAdmitDate"),
        ("inactive_date", "InactiveDate"),
        ("law_school", "LawSchool"),
        ("languages", "Languages"),
        ("jurisdictions", "Jurisdictions"),
        ("specializations", "Specializations"),
        ("sections", "Sections"),
        ("bio", "Bio"),
        ("areas_of_law", "AreasOfLawAndPractice"),
        ("is_pro_bono", "IsProBonoCounsel"),
        ("profile_pic_url", "ProfilePicUrl"),
        ("bio_pic_url", "BioPicUrl"),
    )
    for k, src in detail_only_keys:
        if src in record:
            v = record[src]
            # Skip empties so the JSON stays compact.
            if v in (None, "", [], {}):
                continue
            contact[k] = v
    return contact


def _record_to_firm_dict(
    record: dict[str, Any], source_url: str
) -> dict[str, Any]:
    """One attorney record -> one firm-shaped dict (raw fields only).

    The pipeline normalizes and aggregates across attorneys at the
    same firm/office.

    AZ Bar attorneys sometimes use status placeholders in the Company
    field. We strip those and surface them on `deactivation_status`:

        "Retired"   -> deactivation_status="retired",  name_raw=None
        "Inactive"  -> deactivation_status="inactive", name_raw=None
        "Deceased"  -> deactivation_status="deceased", name_raw=None
        "N/A"       -> name_raw=None  (no deactivation marker — just absent)

    Without this, "Retired" was getting normalized as a firm name and
    aggregation collapsed unrelated attorneys.
    """
    company_raw = record.get("Company")
    if company_raw is not None:
        company_raw = company_raw.strip() or None

    deactivation_status: str | None = None
    if company_raw is not None:
        cleaned = company_raw.strip().lower().rstrip(".")
        if cleaned in _DEACTIVATION_MARKERS:
            deactivation_status = _DEACTIVATION_MARKERS[cleaned]
            company_raw = None
        elif cleaned in _ABSENT_MARKERS:
            # "N/A" and friends: not a firm, not a deactivation either.
            company_raw = None

    contact = _attorney_contact(record)
    office = _office_dict(record.get("Address"))
    offices = [office] if office else []

    practice_areas_raw = _flatten_areas_of_law(record.get("AreasOfLawAndPractice"))

    # Additional source-specific fields preserved in additional_data so
    # the loader/parsing pipeline doesn't have to know about every
    # AZ-Bar-specific column up front.
    additional: dict[str, Any] = {}
    for k in (
        "ABSStatus",
        "PLIStatus",
        "AcceptsCC",
        "ContingencyFee",
        "FixedFee",
        "FreeConsultations",
        "HourlyRate",
        "MemberType",
        "BillCode",
        "FirmLogoURL",
        "LiabilityInsurance",
        "LiabilityInsuranceDescription",
        "LicenseAreas",
    ):
        if k in record:
            v = record[k]
            if v in (None, "", [], {}):
                continue
            additional[k] = v

    return {
        "name_raw": company_raw,
        "deactivation_status": deactivation_status,
        "website_raw": (record.get("FirmURL") or "").strip() or None,
        # Firm-level phone takes the same primary phone as the contact;
        # aggregation may overwrite if a later attorney supplies a
        # better value.
        "phone_raw": contact.get("phone_raw"),
        "year_founded": None,  # not exposed by AZ Bar
        "attorney_count": None,  # set during aggregation
        "source_last_updated_at": None,  # not exposed by AZ Bar
        "contacts": [contact],
        "offices": offices,
        "practice_areas_raw": practice_areas_raw,
        "additional_data": additional,
    }


class AZBarListParser(BaseParser):
    """Parses a paginated list response into per-attorney firm dicts."""

    SOURCE_NAME = "az_bar"

    def parse_bytes(
        self, payload: bytes, *, source_url: str
    ) -> list[dict[str, Any]]:
        envelope = json.loads(payload)
        if not envelope.get("IsSuccess"):
            return []
        result = envelope.get("Result") or {}
        attorneys = result.get("Results") or []
        return [_record_to_firm_dict(a, source_url) for a in attorneys]


class AZBarDetailParser(BaseParser):
    """Parses a single-attorney detail response into one firm dict."""

    SOURCE_NAME = "az_bar"

    def parse_bytes(
        self, payload: bytes, *, source_url: str
    ) -> list[dict[str, Any]]:
        envelope = json.loads(payload)
        if not envelope.get("IsSuccess"):
            return []
        record = envelope.get("Result") or {}
        if not record:
            return []
        return [_record_to_firm_dict(record, source_url)]
