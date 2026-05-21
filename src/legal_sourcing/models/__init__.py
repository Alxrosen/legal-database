"""SQLAlchemy models.

Importing every module here ensures `Base.metadata` knows about all
tables for Alembic autogenerate.
"""

from legal_sourcing.models.base import Base, TimestampMixin
from legal_sourcing.models.firm import Firm, FirmSourceRecordLink
from legal_sourcing.models.office import Office
from legal_sourcing.models.person import FirmPerson, Person
from legal_sourcing.models.practice_area import (
    FirmPracticeArea,
    PracticeArea,
    PracticeAreaAlias,
    UnmatchedPracticeArea,
)
from legal_sourcing.models.review import MatchReviewQueue
from legal_sourcing.models.source_record import FirmSourceRecord

__all__ = [
    "Base",
    "TimestampMixin",
    "Firm",
    "FirmSourceRecord",
    "FirmSourceRecordLink",
    "Office",
    "Person",
    "FirmPerson",
    "PracticeArea",
    "PracticeAreaAlias",
    "FirmPracticeArea",
    "UnmatchedPracticeArea",
    "MatchReviewQueue",
]
