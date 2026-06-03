"""Per-state bar-directory configs for the generic state-bar scraper.

One general scraper (see ``scrapers/state_bar.py`` + ``pipelines/scrape_state_bar.py``)
runs against any of these configs rather than a bespoke scraper per state.
Each ``StateBarConfig`` declares the *request/sweep/pagination* shape; the
messy bits (turning a result page into rows, and a detail page into fields)
are small named extractor functions in ``parsers/state_bar.py`` so we don't
maintain 50 fragile codebases — the variation is data, the spine is shared.

Recon that produced these lives in ``scripts/recon_state_bars.py`` +
``scripts/recon_state_bars_forms.py`` and ``docs/data_sources/state_bars.md``.

State bars are PERSON-level (one row per attorney). Most expose firm /
address / phone only on a per-attorney DETAIL page (the AZ Bar shape), so the
default flow is list-sweep -> detail-fetch -> aggregate-to-firm. A state whose
list rows already carry every useful field can set ``needs_detail=False``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StateBarConfig:
    # ---- identity ----
    source: str  # FirmSourceRecord.source, e.g. "wy_bar"
    state: str  # USPS 2-letter, e.g. "WY"
    name: str  # human label, e.g. "Wyoming State Bar"
    base_url: str  # scheme+host, used for robots + URL joins

    # ---- LIST search request ----
    list_url: str  # absolute URL the search submits to
    list_extractor: str  # key into parsers.state_bar._LIST_EXTRACTORS
    list_method: str = "GET"  # GET | POST
    static_fields: Mapping[str, str] = field(default_factory=dict)  # always-sent params/fields
    search_field: str = ""  # the field varied across the sweep (e.g. "lastname")

    # ---- sweep strategy (how we enumerate the whole directory) ----
    # "alpha"  -> last-name prefixes a..z (26 searches)
    # "alpha2" -> aa..zz (676 searches) for large states with per-query caps
    # "single" -> one broad search (search_field unused / empty)
    sweep: str = "alpha"

    # ---- pagination WITHIN one search's results ----
    # "none"      -> all results on one page
    # "page"      -> add {page_field: n}, n=1.. until a short/empty page
    # "range"     -> cv5 style: {page_field: "start/size"} (1/25, 26/25, ...)
    # "next_link" -> follow next_link_selector's href until absent
    pagination: str = "none"
    page_field: str | None = None
    page_size: int | None = None
    next_link_selector: str | None = None
    max_pages_per_search: int = 80  # safety stop

    # ---- detail ----
    needs_detail: bool = True
    detail_extractor: str | None = None  # key into parsers.state_bar._DETAIL_EXTRACTORS

    # ---- politeness (overrides StateBarScraper defaults) ----
    rps: float = 0.5
    initial_rps: float | None = 0.33
    ramp_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.list_method.upper() not in ("GET", "POST"):
            raise ValueError(f"{self.source}: list_method must be GET|POST")
        if self.sweep not in ("alpha", "alpha2", "single"):
            raise ValueError(f"{self.source}: unknown sweep {self.sweep!r}")
        if self.pagination not in ("none", "page", "range", "next_link"):
            raise ValueError(f"{self.source}: unknown pagination {self.pagination!r}")
        if self.needs_detail and not self.detail_extractor:
            raise ValueError(f"{self.source}: needs_detail but no detail_extractor")


# ---------------------------------------------------------------------------
# Registry. Add a state here + its extractors in parsers/state_bar.py.

WYOMING = StateBarConfig(
    source="wy_bar",
    state="WY",
    name="Wyoming State Bar",
    base_url="https://www.wyomingbar.org",
    list_url="https://www.wyomingbar.org/for-the-public/hire-a-lawyer/lawyer-search/",
    list_method="GET",
    # The lawyer-search form (NOT the site-wide search box that carries the
    # reCAPTCHA) is a plain GET; searchtype=az + advanced=true select the
    # last-name-prefix mode, sort=lastname stabilizes order.
    static_fields={"searchtype": "az", "advanced": "true", "sort": "lastname"},
    search_field="lastname",
    sweep="alpha",  # lastname is a starts-with filter -> a..z covers everyone
    pagination="none",  # one page per letter (verified small; guard logs if huge)
    list_extractor="wy_list",
    needs_detail=True,
    detail_extractor="wy_detail",
)


CONFIGS: dict[str, StateBarConfig] = {
    c.source.split("_")[0]: c  # key by state abbr ("wy")
    for c in (WYOMING,)
}


def get_config(state_abbr: str) -> StateBarConfig:
    key = state_abbr.strip().lower()
    if key not in CONFIGS:
        raise KeyError(f"No state-bar config for {state_abbr!r}. Known: {sorted(CONFIGS)}")
    return CONFIGS[key]


__all__ = ["CONFIGS", "WYOMING", "StateBarConfig", "get_config"]
