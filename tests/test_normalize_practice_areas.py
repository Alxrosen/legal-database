"""Practice-area normalization + matching tests."""

from __future__ import annotations

import pytest

from legal_sourcing.normalize.practice_areas import (
    get_taxonomy,
    normalize_practice_area,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Personal Injury", "personal injury"),
        ("Personal Injury Law", "personal injury"),
        ("Personal-Injury Attorneys", "personal injury"),
        ("Car Crashes", "car crash"),
        ("AUTO ACCIDENTS!!!", "auto accident"),
        # Filler-only inputs collapse to None
        ("Law", None),
        ("Legal Services", None),
        ("Attorney", None),
        # Empty
        ("", None),
        (None, None),
    ],
)
def test_normalize_practice_area(raw, expected):
    assert normalize_practice_area(raw) == expected


def test_taxonomy_matches_personal_injury_aliases():
    tax = get_taxonomy()
    for raw in ("Personal Injury", "Personal Injury Law", "PI", "injuries"):
        slug = tax.match(raw)
        assert slug == "personal-injury", f"{raw!r} did not match personal-injury"


def test_taxonomy_matches_auto_accident_aliases():
    tax = get_taxonomy()
    for raw in ("Auto Accidents", "Car Crash", "MVA", "Motor Vehicle Accident"):
        assert tax.match(raw) == "auto-accidents", raw


def test_taxonomy_rollup_chain():
    tax = get_taxonomy()
    chain = tax.rollup_chain("auto-accidents")
    assert chain == ["auto-accidents", "personal-injury"]


def test_taxonomy_unmatched_returns_none():
    tax = get_taxonomy()
    assert tax.match("Tax Law") is None  # we have no canonical "tax" yet
    assert tax.match("blockchain stuff") is None


def test_taxonomy_priority_flag_set():
    tax = get_taxonomy()
    assert tax.areas["personal-injury"].priority is True
    assert tax.areas["auto-accidents"].priority is True
    # Non-priority area
    assert tax.areas["immigration"].priority is False
