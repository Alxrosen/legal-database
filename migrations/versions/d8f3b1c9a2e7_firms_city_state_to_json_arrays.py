"""firms: city/state -> JSON arrays (ALL offices, not just the dominant one)

Revision ID: d8f3b1c9a2e7
Revises: c7e2a9d4f1b8
Create Date: 2026-06-16 14:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8f3b1c9a2e7'
down_revision: Union[str, None] = 'c7e2a9d4f1b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # city/state become JSON arrays holding ALL distinct offices (a firm spans
    # many offices), replacing the single dominant value. Drop the scalar columns
    # + their (now-useless) indexes and add JSON columns of the same name. No data
    # preservation needed: `firms` is fully rebuilt by `apply --splink`.
    op.drop_index(op.f("ix_firms_city"), table_name="firms")
    op.drop_index(op.f("ix_firms_state"), table_name="firms")
    op.drop_column("firms", "city")
    op.drop_column("firms", "state")
    op.add_column("firms", sa.Column("city", sa.JSON(), nullable=True))
    op.add_column("firms", sa.Column("state", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("firms", "state")
    op.drop_column("firms", "city")
    op.add_column("firms", sa.Column("city", sa.String(length=128), nullable=True))
    op.add_column("firms", sa.Column("state", sa.String(length=8), nullable=True))
    op.create_index(op.f("ix_firms_city"), "firms", ["city"], unique=False)
    op.create_index(op.f("ix_firms_state"), "firms", ["state"], unique=False)
