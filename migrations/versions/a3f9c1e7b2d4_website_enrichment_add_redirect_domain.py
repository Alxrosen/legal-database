"""website_enrichment: add redirect_domain

Revision ID: a3f9c1e7b2d4
Revises: 6609f2e34a48
Create Date: 2026-06-15 12:00:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f9c1e7b2d4'
down_revision: Union[str, None] = '6609f2e34a48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add-only change: native ALTER TABLE ADD COLUMN (no batch table rebuild) so
    # it is metadata-only and safe to apply while the live scrape / website
    # enrichment are writing to the shared database.
    op.add_column("website_enrichment", sa.Column("redirect_domain", sa.String(length=512), nullable=True))
    op.create_index(
        op.f("ix_website_enrichment_redirect_domain"), "website_enrichment", ["redirect_domain"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_website_enrichment_redirect_domain"), table_name="website_enrichment")
    op.drop_column("website_enrichment", "redirect_domain")
