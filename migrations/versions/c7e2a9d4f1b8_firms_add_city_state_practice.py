"""firms: add city, state, practice_areas

Revision ID: c7e2a9d4f1b8
Revises: a3f9c1e7b2d4
Create Date: 2026-06-15 20:30:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e2a9d4f1b8'
down_revision: Union[str, None] = 'a3f9c1e7b2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add-only change: native ALTER TABLE ADD COLUMN (metadata-only, safe during
    # live writes). Promotes location + practice-area signals onto the canonical
    # firm record (populated at fusion time; see resolution/fusion.py).
    op.add_column("firms", sa.Column("city", sa.String(length=128), nullable=True))
    op.add_column("firms", sa.Column("state", sa.String(length=8), nullable=True))
    op.add_column("firms", sa.Column("practice_areas", sa.JSON(), nullable=True))
    op.create_index(op.f("ix_firms_city"), "firms", ["city"], unique=False)
    op.create_index(op.f("ix_firms_state"), "firms", ["state"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_firms_state"), table_name="firms")
    op.drop_index(op.f("ix_firms_city"), table_name="firms")
    op.drop_column("firms", "practice_areas")
    op.drop_column("firms", "state")
    op.drop_column("firms", "city")
