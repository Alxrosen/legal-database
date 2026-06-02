"""FirmSourceRecord: one row per (source, firm-as-that-source-sees-it).

Grain decision: firm-level. Contacts (attorneys at this firm) and offices
(multiple addresses) are stored as JSON lists on the record itself so we
keep the source's complete view in a single row. The JSON shapes are
documented below and are deliberately stable so splitting them out into
their own tables later is a mechanical migration.

Normalization principle: every raw field has a companion `*_normalized`
field. Normalization is for comparison/blocking, never for display. Both
are preserved on the record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from legal_sourcing.models.base import Base, TimestampMixin


class FirmSourceRecord(Base, TimestampMixin):
    """One source's view of one firm.

    JSON shapes (kept stable for future migration to child tables):

    contacts: list of {
        "name_raw": str,
        "name_normalized": str | null,
        "first_name": str | null,
        "last_name": str | null,
        "title": str | null,          # e.g. "Partner", "Of Counsel"
        "email_raw": str | null,
        "email_normalized": str | null,
        "phone_raw": str | null,
        "phone_normalized": str | null,   # E.164
        "bar_number": str | null,
        "bar_state": str | null,
    }

    offices: list of {
        "label": str | null,          # e.g. "Main Office", "Phoenix HQ"
        "is_primary": bool,
        "street_raw": str | null,
        "street2_raw": str | null,
        "city_raw": str | null,
        "state_raw": str | null,
        "postal_code_raw": str | null,
        "country_raw": str | null,
        "normalized": {                # produced by usaddress + cleanup
            "street": str | null,
            "city": str | null,
            "state": str | null,       # 2-letter
            "postal_code": str | null, # 5-digit
            "country": str | null,
        } | null,
        "phone_raw": str | null,
        "phone_normalized": str | null,
        "lat": float | null,
        "lng": float | null,
    }

    practice_areas_raw: list of source-supplied strings (verbatim).
    practice_areas_matched: list of canonical PracticeArea.slug values.
    practice_areas_unmatched: list of normalized strings that did not match.
    """

    __tablename__ = "firm_source_records"
    __table_args__ = (
        # A source's own ID for a firm is the unique dedupe key.
        # We do NOT enforce uniqueness on (source, source_url) because
        # aggregated firms from sources like Martindale share a common
        # city-listing URL; the URL is informational, not identity.
        UniqueConstraint("source", "source_firm_id", name="uq_source_record_source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Source identification
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_firm_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)

    # Scrape provenance
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Firm identity fields (raw + normalized)
    name_raw: Mapped[str] = mapped_column(String(512), nullable=False)
    name_normalized: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)

    website_raw: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    website_normalized: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)

    phone_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone_normalized: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # Firm metadata (nullable — most sources won't supply these).
    year_founded: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attorney_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Set when the source's firm-name field is a status placeholder
    # (e.g. "Retired", "Inactive", "Deceased") instead of an actual
    # firm. In that case name_raw is nulled and this captures the
    # word so we can filter / report on dormant entries without
    # losing the signal. Lowercase canonical form ("retired" /
    # "inactive" / "deceased" / source-specific synonyms).
    deactivation_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # Source-reported "last updated" / publish date for this firm's entry.
    # Distinct from `scraped_at` (when *we* fetched the page).
    source_last_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Primary-office denormalization ---------------------------------
    # Derived from offices[i where is_primary=True]; queryable directly.
    # Indexed because real query targets ("firms in Phoenix, AZ") hit
    # these. We deliberately skip primary_street (low-cardinality for
    # indexing) and primary_country (almost always "US").
    primary_city: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    primary_state: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    primary_postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # ---- Firm-profile enrichment fields ---------------------------------
    # Populated by the Martindale firm-profile enrichment pass. Other
    # sources may use these too if the underlying page exposes them.
    office_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_subscriber: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("0"), index=True
    )
    firm_short_description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # List of {"heading": str|None, "text": str} — separate
    # office-specific blurbs from firm-wide ones.
    firm_descriptions: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # 'pending' (not yet attempted) / 'enriched' / 'no_profile' (no
    # firm_profile_url) / 'failed' (fetch or parse error). Drives the
    # resumable enrichment pass.
    enrichment_status: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)

    # Composite / multi-valued fields as JSON. See docstring for shapes.
    # server_default mirrors the Python-side default so raw-SQL inserts
    # also produce non-NULL, well-shaped JSON.
    contacts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    offices: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )

    practice_areas_raw: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    practice_areas_matched: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    practice_areas_unmatched: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )

    # Escape hatch for source-specific data we don't have a column for yet.
    additional_data: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
