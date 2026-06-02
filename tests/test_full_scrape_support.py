"""Tests for the national-scrape support layer.

Covers the load-bearing pure functions added for `full`/`load` mode:
  * geo.parse_states_arg — what "national" actually expands to.
  * Checkpoint — resumability persistence (the AZ-Bar-crash lesson).
  * city-slug discovery regexes for Martindale and FindLaw — these
    decide which cities a national run even visits, so a regex slip is
    a silent coverage hole.
"""

from __future__ import annotations

import pytest

from legal_sourcing.geo import US_STATE_SLUGS, parse_states_arg
from legal_sourcing.pipelines import scrape_findlaw as fl
from legal_sourcing.pipelines import scrape_martindale as md
from legal_sourcing.pipelines._checkpoint import Checkpoint

# ---- geo.parse_states_arg -------------------------------------------------


def test_states_arg_all_variants_expand_to_full_list():
    assert parse_states_arg("all") == list(US_STATE_SLUGS)
    assert parse_states_arg(None) == list(US_STATE_SLUGS)
    assert parse_states_arg("") == list(US_STATE_SLUGS)
    assert parse_states_arg("  ") == list(US_STATE_SLUGS)
    assert len(US_STATE_SLUGS) == 51  # 50 + DC


def test_states_arg_comma_list_and_normalization():
    assert parse_states_arg("arizona,new-york") == ["arizona", "new-york"]
    # Case + surrounding whitespace tolerated.
    assert parse_states_arg(" Arizona , TEXAS ") == ["arizona", "texas"]


def test_states_arg_unknown_slug_fails_loud():
    with pytest.raises(ValueError):
        parse_states_arg("arizona,not-a-state")


# ---- Checkpoint -----------------------------------------------------------


class _FakeSettings:
    def __init__(self, processed_dir):
        self.processed_data_dir = processed_dir


def _patch_checkpoint_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "legal_sourcing.pipelines._checkpoint.get_settings",
        lambda: _FakeSettings(tmp_path / "processed"),
    )


def test_checkpoint_persists_and_reloads(monkeypatch, tmp_path):
    _patch_checkpoint_dir(monkeypatch, tmp_path)
    cp = Checkpoint("unit_test")
    assert not cp.is_done("arizona/phoenix")
    cp.mark_done("arizona/phoenix", inserted=3, updated=1)
    cp.mark_done("arizona/tucson", inserted=2, updated=0)

    # A fresh instance must see the persisted progress (resume path).
    cp2 = Checkpoint("unit_test")
    assert cp2.is_done("arizona/phoenix")
    assert cp2.is_done("arizona/tucson")
    assert not cp2.is_done("texas/austin")
    assert cp2.completed_count == 2
    assert cp2.totals == {"inserted": 5, "updated": 1, "cities": 2}


def test_checkpoint_file_is_valid_json(monkeypatch, tmp_path):
    import json

    _patch_checkpoint_dir(monkeypatch, tmp_path)
    cp = Checkpoint("json_check")
    cp.mark_done("a/b")
    data = json.loads(cp.path.read_text(encoding="utf-8"))
    assert data["completed"] == ["a/b"]
    assert data["name"] == "json_check"


# ---- Martindale city discovery -------------------------------------------

_MD_STATE_HTML = """
<html><body>
  <a href="https://www.martindale.com/all-lawyers/phoenix/arizona/">Phoenix</a>
  <a href="/all-lawyers/tucson/arizona/">Tucson</a>
  <a href="/all-lawyers/apache-junction/arizona/">Apache Junction</a>
  <!-- wrong state: must be ignored -->
  <a href="/all-lawyers/dallas/texas/">Dallas</a>
  <!-- not a city page: must be ignored -->
  <a href="/by-location/arizona-lawyers/">All AZ</a>
  <a href="/legal-news/">News</a>
  <!-- deeper path under a city: must not match -->
  <a href="/all-lawyers/phoenix/arizona/family-law/">PI in Phoenix</a>
</body></html>
"""


def test_martindale_extract_city_slugs():
    got = md.extract_city_slugs(_MD_STATE_HTML, "arizona")
    assert got == ["apache-junction", "phoenix", "tucson"]


# ---- FindLaw city discovery ----------------------------------------------

_FL_STATE_HTML = """
<html><body>
  <a href="/personal-injury-plaintiff/arizona/phoenix/">Phoenix</a>
  <a href="https://lawyers.findlaw.com/personal-injury-plaintiff/arizona/mesa/">Mesa</a>
  <!-- aggregation links that share the shape but aren't cities -->
  <a href="/personal-injury-plaintiff/arizona/all-cities/">All Cities</a>
  <a href="/personal-injury-plaintiff/arizona/maricopa-county/">Maricopa County</a>
  <!-- different practice area / state: must be ignored -->
  <a href="/medical-malpractice/arizona/phoenix/">MedMal Phoenix</a>
  <a href="/personal-injury-plaintiff/texas/houston/">Houston</a>
</body></html>
"""


def test_findlaw_extract_city_slugs_filters_aggregation_pages():
    got = fl.extract_city_slugs(_FL_STATE_HTML, "personal-injury-plaintiff", "arizona")
    # all-cities and *-county dropped; other PA/state ignored.
    assert got == ["mesa", "phoenix"]
