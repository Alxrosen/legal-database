"""Practice-area taxonomy and firm tags.

Source of truth for the canonical taxonomy is
`data/reference/practice_areas.yaml`. A seed script (added in M3 alongside
the normalize utilities) loads that file into these tables.

Matching policy (per project decision):
  * Normalize both source-supplied strings and alias strings the same way
    (lowercase, trim, collapse whitespace, strip filler words, strip
    punctuation, singularize).
  * Match by EXACT equality on the normalized string. No fuzzy matching.
  * Unmatched values are preserved on the source record AND counted in
    UnmatchedPracticeArea for periodic review.

Subcategory roll-up: PracticeArea has a parent_id. A firm tagged with a
child (e.g. "Auto Accidents") is implicitly tagged with the parent
("Personal Injury"). Roll-up is a query-time concern; we do NOT
double-write rows for the parent.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from legal_sourcing.models.base import Base, TimestampMixin


class PracticeArea(Base, TimestampMixin):
    __tablename__ = "practice_areas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Subcategories roll up to their parent.
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("practice_areas.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Marks categories the business cares about. Personal Injury is the
    # first priority; downstream code can filter on this flag.
    priority: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PracticeAreaAlias(Base, TimestampMixin):
    """Alias strings that map to a canonical PracticeArea.

    Both `alias` (display form) and `alias_normalized` (matching form) are
    stored. Lookup happens on `alias_normalized`.
    """

    __tablename__ = "practice_area_aliases"
    __table_args__ = (UniqueConstraint("alias_normalized", name="uq_alias_normalized"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    practice_area_id: Mapped[int] = mapped_column(
        ForeignKey("practice_areas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    alias: Mapped[str] = mapped_column(String(256), nullable=False)
    alias_normalized: Mapped[str] = mapped_column(String(256), nullable=False, index=True)


class FirmPracticeArea(Base, TimestampMixin):
    """A canonical firm's tagging with a canonical practice area.

    `source_record_id` records which source supplied the tag (provenance).
    A firm/area pair may appear once per source — if the same area arrives
    from multiple sources, we keep one row per source for auditability.
    """

    __tablename__ = "firm_practice_areas"
    __table_args__ = (
        UniqueConstraint(
            "firm_id",
            "practice_area_id",
            "source_record_id",
            name="uq_firm_practice_area_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    firm_id: Mapped[int] = mapped_column(
        ForeignKey("firms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    practice_area_id: Mapped[int] = mapped_column(
        ForeignKey("practice_areas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("firm_source_records.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class UnmatchedPracticeArea(Base, TimestampMixin):
    """A normalized practice-area string that did not match any alias.

    One row per distinct `normalized_value`; `count` increments on each
    re-encounter. Reviewed periodically — we either add aliases to an
    existing PracticeArea or introduce a new canonical area.
    """

    __tablename__ = "unmatched_practice_areas"
    __table_args__ = (UniqueConstraint("normalized_value", name="uq_unmatched_normalized"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    raw_value: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sample_source_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("firm_source_records.id", ondelete="SET NULL"), nullable=True
    )
