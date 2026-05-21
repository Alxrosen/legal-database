"""Canonical Person (attorney) and the firm-person link.

Source records hold contacts inline as JSON. Resolution promotes contacts
to canonical Person rows and wires them to canonical Firms via FirmPerson.
The link is many-to-many over time: a lawyer may move firms, so we keep
start/end dates and an `active` flag.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, String, UniqueConstraint
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
    """Affiliation of a Person with a Firm. Time-bounded."""

    __tablename__ = "firm_persons"
    __table_args__ = (
        UniqueConstraint("firm_id", "person_id", "start_date", name="uq_firm_person"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    firm_id: Mapped[int] = mapped_column(
        ForeignKey("firms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
