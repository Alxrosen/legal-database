"""Canonical Firm and the link table that wires source records to it."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from legal_sourcing.models.base import Base, TimestampMixin


class Firm(Base, TimestampMixin):
    """The canonical firm.

    Fields here represent the system's best current answer for each
    attribute, chosen from one or more source records. `field_provenance`
    records which source record each field came from — kept as a JSON
    column so normal queries on Firm aren't affected.

    field_provenance shape:
        {
            "name":    {"source_record_id": int, "source": str, "written_at": iso8601},
            "website": {"source_record_id": int, "source": str, "written_at": iso8601},
            "phone":   {...},
            ...
        }

    Per-field provenance lets us re-run precedence rules (e.g. "state bar
    beats firm website for name") without losing the history of which
    source supplied what.
    """

    __tablename__ = "firms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Canonical chosen values — raw + normalized.
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    name_normalized: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)

    website: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    website_normalized: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)

    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone_normalized: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # Firm metadata (nullable).
    year_founded: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attorney_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ALL distinct office locations across the cluster (JSON arrays, sorted) — a
    # firm spans many offices, so these hold every city/state, not just the HQ.
    # e.g. state=["AZ","CA"], city=["Phoenix","San Diego"]. none_as_null so empty
    # is SQL NULL. NULL = not derivable.
    city: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    state: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    # Canonical PracticeArea.slug values surfaced across the firm's records
    # (UNION — a firm covers all of them). NULL = none extracted.
    practice_areas: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    # Free-form per-field provenance.
    field_provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )


class FirmSourceRecordLink(Base, TimestampMixin):
    """Many-to-many: a canonical firm can be backed by many source records,
    and (rarely, in error cases pre-review) a source record can be
    associated with multiple firms.
    """

    __tablename__ = "firm_source_record_links"
    __table_args__ = (
        UniqueConstraint("firm_id", "firm_source_record_id", name="uq_firm_source_record_link"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    firm_id: Mapped[int] = mapped_column(
        ForeignKey("firms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    firm_source_record_id: Mapped[int] = mapped_column(
        ForeignKey("firm_source_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # How the link was made: 'auto' (above threshold) or 'manual' (review queue).
    link_method: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")

    # The resolution decision that produced this link, if any.
    match_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("match_review_queue.id", ondelete="SET NULL"), nullable=True
    )
