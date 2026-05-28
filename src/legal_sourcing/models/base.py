"""SQLAlchemy declarative base and shared mixins."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Project-wide declarative base."""


class TimestampMixin:
    """Adds created_at / updated_at columns. UTC.

    Python-side defaults fire on ORM inserts; `server_default=func.now()`
    guards against raw-SQL inserts (and `server_onupdate` does the same
    for raw updates). Both layers stay in sync.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
        nullable=False,
    )
