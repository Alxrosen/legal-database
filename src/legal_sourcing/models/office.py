"""Canonical Office — a physical location belonging to a canonical Firm.

Source records hold offices inline as JSON; resolution creates / updates
canonical Office rows by matching addresses across source records belonging
to the same Firm.
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from legal_sourcing.models.base import Base, TimestampMixin


class Office(Base, TimestampMixin):
    __tablename__ = "offices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    firm_id: Mapped[int] = mapped_column(
        ForeignKey("firms.id", ondelete="CASCADE"), nullable=False, index=True
    )

    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_headquarters: Mapped[bool] = mapped_column(default=False, nullable=False)

    street_raw: Mapped[str | None] = mapped_column(String(512), nullable=True)
    street_normalized: Mapped[str | None] = mapped_column(String(512), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True, default="US")

    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone_normalized: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
