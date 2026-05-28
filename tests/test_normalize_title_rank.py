"""Title-rank classifier tests."""

from __future__ import annotations

import pytest

from legal_sourcing.normalize.title_rank import classify_title


@pytest.mark.parametrize(
    "title, expected",
    [
        # 100 — top leadership
        ("Managing Partner", 100),
        ("MANAGING DIRECTOR", 100),
        # 90 — name-on-door
        ("Senior Partner", 90),
        ("Equity Partner", 90),
        ("Founding Partner", 90),
        ("Founder", 90),
        # 80 — partner / principal
        ("Partner", 80),
        ("Principal", 80),
        # 70 — counsel variants (specific before generic)
        ("Of Counsel", 70),
        ("Senior Counsel", 70),
        # 60 — generic counsel
        ("Counsel", 60),
        # 50 — senior associate
        ("Senior Associate", 50),
        ("Senior Attorney", 50),
        # 40 — associate (must come BEFORE 30 specifics in this test)
        ("Associate", 40),
        # 30 — junior / staff (must NOT collapse to 40)
        ("Junior Associate", 30),
        ("Staff Attorney", 30),
        # 20 — generic attorney / lawyer
        ("Attorney at Law", 20),
        ("Lawyer", 20),
        # Filler-only / unknown
        ("Esq.", None),
        ("", None),
        (None, None),
        ("Receptionist", None),
    ],
)
def test_classify_title(title, expected):
    assert classify_title(title) == expected


def test_junior_associate_outranks_associate_negatively():
    """Regression: Junior Associate must classify as 30, not 40."""
    assert classify_title("Junior Associate") == 30
    assert classify_title("Associate") == 40
    assert classify_title("Junior Associate") < classify_title("Associate")
