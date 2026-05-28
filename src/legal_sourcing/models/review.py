"""Match review queue: candidate pairs awaiting a merge decision.

Every resolution decision — auto-merge, manual-merge, reject — is stored
here with the score components that produced it. This makes the matching
step re-runnable with new thresholds without re-scraping.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from legal_sourcing.models.base import Base, TimestampMixin


class MatchReviewQueue(Base, TimestampMixin):
    __tablename__ = "match_review_queue"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending','approved','rejected','auto_approved')",
            name="ck_match_review_status",
        ),
        CheckConstraint(
            "source_record_a_id < source_record_b_id",
            name="ck_match_review_ordered_pair",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Ordered so each unordered pair appears once.
    source_record_a_id: Mapped[int] = mapped_column(
        ForeignKey("firm_source_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_record_b_id: Mapped[int] = mapped_column(
        ForeignKey("firm_source_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    score_total: Mapped[float] = mapped_column(Float, nullable=False)
    # {"name": 0.92, "phone": 1.0, "website": 0.0, "address": 0.8, "people": 0.5, ...}
    score_components: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )

    # Snapshot of the thresholds in effect when the row was created.
    thresholds: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    reviewer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewer_notes: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Populated when this decision results in (or refers to) a canonical firm.
    resulting_firm_id: Mapped[int | None] = mapped_column(
        ForeignKey("firms.id", ondelete="SET NULL"), nullable=True
    )
