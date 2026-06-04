"""WebsiteEnrichment: one row per UNIQUE firm website (normalized).

A website maps to MANY source records (e.g. forthepeople.com -> hundreds) and
eventually one canonical Firm, so we crawl + extract each unique site ONCE and
reference it many times — keyed by the normalized (bare-domain) website, NOT a
per-FirmSourceRecord column (see docs/assumptions.md 2026-06-02).

Re-runs are a clean set-difference: enrich the DISTINCT `website_normalized`
from `firm_source_records` that are absent here or whose `enriched_at` is stale.
`enriched_at IS NULL` means never successfully enriched (or unreachable). This
is separate from Martindale *profile* enrichment (FirmSourceRecord.enrichment_status).

Populated by the firm-website extraction cascade (enrichment/website_extract.py)
run by the producer/worker pipeline (pipelines/enrich_websites.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from legal_sourcing.models.base import Base, TimestampMixin


class WebsiteEnrichment(Base, TimestampMixin):
    __tablename__ = "website_enrichment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # The match key: normalized bare-domain (e.g. "smithlaw.com"). Unique.
    website: Mapped[str] = mapped_column(String(512), nullable=False, unique=True, index=True)
    resolved_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Pages crawled this run: list of {"role": str, "url": str}.
    pages_crawled: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)

    # Legal-relevance gate.
    is_law_related: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    relevance_terms: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    # Headcount (the headline field). _min because "40+" means "at least".
    attorney_count_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attorney_count_is_min: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    attorney_count_method: Mapped[str | None] = mapped_column(String(24), nullable=True)
    attorney_count_confidence: Mapped[str | None] = mapped_column(String(8), nullable=True)
    attorney_count_raw: Mapped[str | None] = mapped_column(String(512), nullable=True)

    staff_count_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    office_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    office_addresses: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)

    # Primary office geo extracted from the site — same typing as
    # FirmSourceRecord.primary_city/_state for cross-source consistency.
    # NULL = not extracted.
    primary_city: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    primary_state: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)

    # Practice areas surfaced on the site. `practice_areas` = canonical
    # PracticeArea.slug values (matched); `practice_areas_raw` = verbatim
    # source strings. NULL = not extracted (mirrors the other optional fields).
    practice_areas: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    practice_areas_raw: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    years_in_operation_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    years_is_min: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )

    scope: Mapped[str | None] = mapped_column(String(16), nullable=True)
    notable_signals: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    phones: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    # Raw first-sentences blurb (extraction) + the later LLM/template description.
    description_blurb: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_generated: Mapped[str | None] = mapped_column(Text, nullable=True)

    # verified | legal_but_mismatched | not_a_law_firm | unreachable | unverified
    url_verification_status: Mapped[str | None] = mapped_column(
        String(24), nullable=True, index=True
    )
    url_verification_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Thin/JS page that needs a headless render to extract (deferred lever).
    needs_render: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0"), index=True
    )

    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_html_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # When the homepage was fetched, and when extraction completed. enriched_at
    # NULL => never enriched / failed — the set-difference re-run target.
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enriched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
