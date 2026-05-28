"""Base parser interface.

Parsers consume raw payloads (gzipped HTML/JSON written by scrapers
under data/raw/{source}/{date}/) and produce structured dicts shaped
like FirmSourceRecord rows. They are PURE functions of the raw input —
no DB access, no network, no clock — so re-parsing yesterday's scrape
is deterministic.

Subclasses override `parse_bytes`; `parse_file` is provided as a
convenience that gunzips the body and reads the JSON sidecar to attach
source URL / scrape timestamp to each emitted dict.

Output dict shape (matches FirmSourceRecord columns):

    {
        "source": "az_bar",
        "source_url": "...",
        "source_firm_id": "..." | None,
        "scraped_at": iso8601 str,
        "raw_payload_path": "...",
        "http_status": 200,
        "name_raw": "Bow Street LLP",
        "website_raw": "https://www.bowstreet.com" | None,
        "phone_raw": "(602) 555-1234" | None,
        "year_founded": int | None,
        "attorney_count": int | None,
        "source_last_updated_at": iso8601 str | None,
        "contacts": [ {...}, ... ],
        "offices": [ {...}, ... ],
        "practice_areas_raw": [ "Personal Injury", ... ],
        "additional_data": { ... },
    }

Normalization (the `*_normalized` columns and matched/unmatched practice
areas) is NOT the parser's job — it happens in the pipeline layer between
parser output and DB upsert, so we can re-normalize without re-parsing.
"""

from __future__ import annotations

import gzip
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterator


class BaseParser(ABC):
    SOURCE_NAME: str = ""

    def __init__(self) -> None:
        if not self.SOURCE_NAME:
            raise ValueError(f"{type(self).__name__} must set SOURCE_NAME")

    @abstractmethod
    def parse_bytes(self, payload: bytes, *, source_url: str) -> list[dict[str, Any]]:
        """Parse a raw payload into zero-or-more source-record dicts.

        Implementations should be pure: deterministic given the payload
        plus source_url. Do not access the network, the clock, or
        random state.
        """

    def parse_file(self, gz_path: Path) -> list[dict[str, Any]]:
        """Read a gzipped payload + its JSON sidecar from `data/raw/...`,
        delegate to `parse_bytes`, and attach scrape metadata to each
        emitted record.
        """
        gz_path = Path(gz_path)
        sidecar_path = gz_path.with_suffix("").with_suffix(".json")
        # ^ strips ".gz" then swaps ext to ".json" so "foo.html.gz" ->
        # "foo.json" (matching the scraper's filename layout).

        with gzip.open(gz_path, "rb") as f:
            payload = f.read()
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))

        records = self.parse_bytes(payload, source_url=sidecar["url"])
        for r in records:
            r.setdefault("source", self.SOURCE_NAME)
            r.setdefault("source_url", sidecar["url"])
            r.setdefault("scraped_at", sidecar["fetched_at"])
            r.setdefault("raw_payload_path", str(gz_path))
            r.setdefault("http_status", sidecar.get("status"))
        return records

    def iter_payloads(self, raw_dir: Path) -> Iterator[Path]:
        """Walk a raw-data directory and yield every gzipped payload path."""
        for p in sorted(Path(raw_dir).rglob("*.gz")):
            yield p
