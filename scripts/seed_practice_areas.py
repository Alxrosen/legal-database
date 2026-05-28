"""Seed / re-seed the practice_areas + practice_area_aliases tables
from data/reference/practice_areas.yaml.

Idempotent: safe to re-run after editing the YAML. Existing rows are
updated (name / parent / priority / description); missing aliases are
inserted; aliases that disappear from the YAML are removed.

Usage:
    uv run python scripts/seed_practice_areas.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running directly via `python scripts/seed_practice_areas.py`.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from legal_sourcing.config import get_settings  # noqa: E402
from legal_sourcing.models import PracticeArea, PracticeAreaAlias  # noqa: E402
from legal_sourcing.normalize.practice_areas import (  # noqa: E402
    Taxonomy,
    load_taxonomy,
    normalize_practice_area,
)
from legal_sourcing.utils.logging import configure_logging, get_logger  # noqa: E402


def seed(session: Session, taxonomy: Taxonomy) -> dict[str, int]:
    """Upsert taxonomy into the DB. Returns a counts summary."""
    log = get_logger(__name__)
    stats = {"areas_inserted": 0, "areas_updated": 0, "aliases_inserted": 0, "aliases_deleted": 0}

    # Pass 1: upsert every area without parent FK (we'll fill parent_id in pass 2).
    slug_to_id: dict[str, int] = {}
    for slug, ta in taxonomy.areas.items():
        existing = session.scalar(select(PracticeArea).where(PracticeArea.slug == slug))
        if existing is None:
            row = PracticeArea(
                slug=slug,
                name=ta.name,
                description=ta.description,
                priority=ta.priority,
            )
            session.add(row)
            session.flush()
            slug_to_id[slug] = row.id
            stats["areas_inserted"] += 1
        else:
            dirty = False
            if existing.name != ta.name:
                existing.name = ta.name
                dirty = True
            if existing.description != ta.description:
                existing.description = ta.description
                dirty = True
            if existing.priority != ta.priority:
                existing.priority = ta.priority
                dirty = True
            slug_to_id[slug] = existing.id
            if dirty:
                stats["areas_updated"] += 1

    # Pass 2: parent FKs (now every slug has an id).
    for slug, ta in taxonomy.areas.items():
        row = session.get(PracticeArea, slug_to_id[slug])
        parent_id = slug_to_id[ta.parent_slug] if ta.parent_slug else None
        if row.parent_id != parent_id:
            row.parent_id = parent_id

    # Pass 3: aliases. For each area, compute the desired (normalized -> raw)
    # set; insert missing, delete extras.
    for slug, ta in taxonomy.areas.items():
        pa_id = slug_to_id[slug]
        desired: dict[str, str] = {}
        for alias_str in (ta.name, *ta.aliases):
            norm = normalize_practice_area(alias_str)
            if norm is None:
                continue
            desired.setdefault(norm, alias_str)

        existing_rows = session.scalars(
            select(PracticeAreaAlias).where(PracticeAreaAlias.practice_area_id == pa_id)
        ).all()
        existing_by_norm = {r.alias_normalized: r for r in existing_rows}

        # Insert missing.
        for norm, raw in desired.items():
            if norm not in existing_by_norm:
                session.add(
                    PracticeAreaAlias(
                        practice_area_id=pa_id,
                        alias=raw,
                        alias_normalized=norm,
                    )
                )
                stats["aliases_inserted"] += 1

        # Delete dropped.
        for norm, row in existing_by_norm.items():
            if norm not in desired:
                session.delete(row)
                stats["aliases_deleted"] += 1

    session.commit()
    log.info("seed.done", **stats)
    return stats


def main() -> None:
    configure_logging()
    settings = get_settings()
    yaml_path = ROOT / "data" / "reference" / "practice_areas.yaml"
    taxonomy = load_taxonomy(yaml_path)

    engine = create_engine(settings.db_url)
    with Session(engine) as session:
        stats = seed(session, taxonomy)
        print(stats)


if __name__ == "__main__":
    main()
