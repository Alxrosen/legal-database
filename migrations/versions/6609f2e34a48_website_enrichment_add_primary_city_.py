"""website_enrichment: add primary_city/state + practice_areas

Revision ID: 6609f2e34a48
Revises: 852fd3019b0b
Create Date: 2026-06-04 15:56:54.366493+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6609f2e34a48'
down_revision: Union[str, None] = '852fd3019b0b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add-only change: use native ALTER TABLE ADD COLUMN (no batch table
    # rebuild) so this is metadata-only and safe to apply while the live
    # Martindale scrape is writing to the shared database.
    op.add_column("website_enrichment", sa.Column("primary_city", sa.String(length=128), nullable=True))
    op.add_column("website_enrichment", sa.Column("primary_state", sa.String(length=8), nullable=True))
    op.add_column("website_enrichment", sa.Column("practice_areas", sa.JSON(), nullable=True))
    op.add_column("website_enrichment", sa.Column("practice_areas_raw", sa.JSON(), nullable=True))
    op.create_index(
        op.f("ix_website_enrichment_primary_city"), "website_enrichment", ["primary_city"], unique=False
    )
    op.create_index(
        op.f("ix_website_enrichment_primary_state"), "website_enrichment", ["primary_state"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_website_enrichment_primary_state"), table_name="website_enrichment")
    op.drop_index(op.f("ix_website_enrichment_primary_city"), table_name="website_enrichment")
    op.drop_column("website_enrichment", "practice_areas_raw")
    op.drop_column("website_enrichment", "practice_areas")
    op.drop_column("website_enrichment", "primary_state")
    op.drop_column("website_enrichment", "primary_city")
