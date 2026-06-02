"""Resumable-run checkpoint.

A national scrape runs for *days*. It must survive a crash, a Ctrl-C,
or a machine reboot without re-fetching everything. The unit of
durable progress is one *city* (Martindale) or one *(state, city)*
FindLaw combo — small enough that a mid-run abort loses at most one
city's work, large enough that the checkpoint file stays tiny.

Each completed unit is appended to a JSON file under
``data/processed/`` as it is committed to the DB. On restart the
pipeline loads the set and skips anything already done. This is the
same lesson as the AZ Bar crash (35,864 pages fetched, DB write lost):
persist progress incrementally, never only at the end.

The file is rewritten atomically (temp file + ``os.replace``) on every
``mark_done`` so a crash mid-write can't corrupt it.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from legal_sourcing.config import get_settings
from legal_sourcing.utils.logging import get_logger

log = get_logger(__name__)


class Checkpoint:
    """A persistent set of completed unit keys for one named run."""

    def __init__(self, name: str) -> None:
        settings = get_settings()
        base = settings.processed_data_dir
        base.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.path: Path = base / f"{name}_progress.json"
        self._completed: set[str] = set()
        self._totals: dict[str, int] = {"inserted": 0, "updated": 0, "cities": 0}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("checkpoint.load_failed", path=str(self.path), error=str(exc))
            return
        self._completed = set(data.get("completed", []))
        totals = data.get("totals") or {}
        for k in self._totals:
            if isinstance(totals.get(k), int):
                self._totals[k] = totals[k]
        log.info(
            "checkpoint.loaded",
            name=self.name,
            completed=len(self._completed),
            **self._totals,
        )

    def is_done(self, key: str) -> bool:
        return key in self._completed

    @property
    def completed_count(self) -> int:
        return len(self._completed)

    @property
    def totals(self) -> dict[str, int]:
        return dict(self._totals)

    def mark_done(self, key: str, *, inserted: int = 0, updated: int = 0) -> None:
        """Record `key` as complete and persist the file atomically."""
        self._completed.add(key)
        self._totals["inserted"] += inserted
        self._totals["updated"] += updated
        self._totals["cities"] += 1
        self._save()

    def _save(self) -> None:
        payload = {
            "name": self.name,
            "updated_at": datetime.now(UTC).isoformat(),
            "totals": self._totals,
            # Sorted for stable diffs / human inspection.
            "completed": sorted(self._completed),
        }
        # Atomic write: temp file in the same dir, then os.replace.
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.name}_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:  # pragma: no cover - disk failure
            log.error("checkpoint.save_failed", path=str(self.path), error=str(exc))
            with contextlib.suppress(OSError):
                os.unlink(tmp)


__all__ = ["Checkpoint"]
