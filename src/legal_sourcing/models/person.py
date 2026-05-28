"""Canonical Person (attorney) and the firm-person link.

Source records hold contacts inline as JSON. Resolution promotes contacts
to canonical Person rows and wires them to canonical Firms via FirmPerson.
The link is many-to-many over time: a lawyer may move firms, so we keep
start/end dates and an `active` flag.

The primary-contact "slot" for a firm is the FirmPerson row with
`is_primary_contact = True`. A partial unique index enforces at most one
primary contact per firm. Replacement is governed by `title_rank`
(higher = more senior); see docs/assumptions.md "Seniority rule for
primary contact".
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from legal_sourcing.models.base import Base, TimestampMixin


class Person(Base, TimestampMixin):
    __tablename__ = "persons"
    __table_args__ = (
        # Bar number is the strongest identity key when present.
        UniqueConstraint("bar_state", "bar_number", name="uq_person_bar"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    full_name: Mapped[str] = mapped_column(String(256), nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    email: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone_normalized: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    bar_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bar_state: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)


class FirmPerson(Base, TimestampMixin):
    """Affiliation of a Person with a Firm. Time-bounded.

    Carries the primary-contact flag and the assessed seniority rank for
    that affiliation's title. Resolution uses `title_rank` to decide
    whether an incoming candidate should replace the current primary
    contact (higher wins; ties / missing rank keep the existing).
    """

    __tablename__ = "firm_persons"
    __table_args__ = (
        UniqueConstraint("firm_id", "person_id", "start_date", name="uq_firm_person"),
        # At most one primary contact per firm. Partial index — SQLite and
        # Postgres both support `WHERE` clauses on unique indexes.
        Index(
            "uq_firm_primary_contact",
            "firm_id",
            unique=True,
            sqlite_where=text("is_primary_contact = 1"),
            postgresql_where=text("is_primary_contact = true"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    firm_id: Mapped[int] = mapped_column(
        ForeignKey("firms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Assessed seniority for the affiliation's title. Populated by the
    # title-rank normalizer (M3). Higher = more senior. NULL = unknown.
    # See docs/assumptions.md "Seniority rule for primary contact" for
    # the ladder.
    title_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    is_primary_contact: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )

    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
