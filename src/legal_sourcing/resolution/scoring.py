"""Pairwise scoring for entity resolution.

`score_pair(a, b)` returns a dict with one component per signal plus a
weighted total in [0, 100]. Components are stored alongside the
decision in `MatchReviewQueue.score_components` so the same
candidate set can be re-decided with new weights/thresholds without
re-scraping or re-scoring (the score is the data, the decision is
the policy).

Components:

  * `name_sim`     — rapidfuzz `token_set_ratio` on `name_normalized`,
                     mapped to [0, 1]. Token-set ratio survives word
                     reordering and minor variations.
  * `phone_exact`  — 1.0 if both records have phones AND they match;
                     0.0 if both have phones but differ; None if
                     either is missing (omitted from the score).
  * `website_exact` — same logic, on the bare-domain
                     `website_normalized`.
  * `city_match`   — 1.0 if both `primary_city` match (case-insensitive);
                     None when either is missing.
  * `state_match`  — 1.0 / 0.0 / None, on `primary_state`.
  * `suffix_diff_penalty` — -1.0 when the entity suffix differs
                     (LLP vs PC); 0.0 otherwise. Treated as a small
                     negative signal: same name with different
                     suffix is *evidence* of two distinct registered
                     entities at one practice.

Weighting:
  total = sum(weight[component] * value)  for each non-None component
  clipped to [0, 100].

Defaults pick a balanced max of ~100 when all components are present:
  name=50, phone=25, website=20, city=10, state=5, suffix=-5
  -> max = 50 + 25 + 20 + 10 + 5 = 110, capped to 100.

Override weights by passing your own dict to `score_pair(..., weights=...)`.
"""

from __future__ import annotations

from typing import Any

from rapidfuzz import fuzz

from legal_sourcing.models import FirmSourceRecord
from legal_sourcing.resolution.identity import is_firm_name, is_identity_website

DEFAULT_WEIGHTS: dict[str, float] = {
    # Weights chosen so all five positive components sum to exactly 100,
    # which means a perfect-everything pair lands at the auto-merge
    # ceiling without the suffix-penalty being silently clipped.
    "name_sim": 45.0,
    "phone_exact": 25.0,
    "website_exact": 20.0,
    "city_match": 5.0,
    "state_match": 5.0,
    # Penalty for suffix mismatch; weight is positive but the value
    # is -1 when applied so the total nudges DOWN.
    "suffix_diff_penalty": 5.0,
}

# A shared *identity* website (not an aggregator / website-builder platform
# domain) is, in this corpus, a near-certain same-firm signal: every
# non-aggregator domain in the DB maps to exactly one firm (verified on
# swlaw.com, forthepeople.com, kutakrock.com, ...). So a website-exact match
# floors the pair into the auto-merge band even when office phones differ or a
# name is missing -- which otherwise shatters multi-office firms (Snell &
# Wilmer fragmented into 38 singletons before this).
WEBSITE_MERGE_FLOOR = 88.0
# ...UNLESS both records carry clearly-different FIRM names (two real firms that
# happen to share one domain): then cap the score out of the auto band so a
# human reviews it.
NAME_CONFLICT_SIM = 0.40
NAME_CONFLICT_CAP = 55.0

# Same phone + a strongly-matching name (same office line AND same name) is also
# decisive -- and it's the only strong signal for the ~83% of records that carry
# no website. Floor it into the auto band. The name requirement guards against
# shared lead-gen / toll-free numbers: different firms have different names, so
# the floor won't fire for them.
PHONE_NAME_FLOOR = 86.0
STRONG_NAME_SIM = 0.85
# A strongly-matching name in the SAME city + state is the same firm even with no
# shared website/phone -- a firm's records across sources/offices often lack a
# common identifier (e.g. Dickinson Wright's Phoenix records). Needs the
# primary_city/primary_state backfill populated; uses a higher name bar than the
# phone floor since city+state is weaker corroboration than an exact phone.
NAME_LOCATION_FLOOR = 86.0
NAME_SIM_FOR_LOCATION = 0.90
# Two records with DIFFERENT identity websites are different firms; cap the score
# so a coincidental name or phone match can't auto-merge them.
WEBSITE_CONFLICT_CAP = 50.0


def _name_suffix(record: FirmSourceRecord) -> str | None:
    """Entity suffix is preserved in the contacts/aggregation path
    only as part of name_raw. We don't carry a separate column today,
    so derive it on the fly from name_raw — strip leading words until
    we hit one of the known suffixes.
    """
    raw = (record.name_raw or "").strip().lower()
    if not raw:
        return None
    # Last 1-2 tokens after stripping punctuation.
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in raw)
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return None
    suffixes = {
        "llp",
        "lllp",
        "llc",
        "pllc",
        "pc",
        "pa",
        "p.a",
        "p.c",
        "plc",
        "ltd",
        "inc",
        "corp",
    }
    # Two-token check (e.g. "p a" / "p c").
    if len(tokens) >= 2 and (tokens[-2] + " " + tokens[-1]) in {"p a", "p c"}:
        return tokens[-2] + " " + tokens[-1]
    if tokens[-1] in suffixes:
        return tokens[-1]
    return None


def score_pair(
    a: FirmSourceRecord,
    b: FirmSourceRecord,
    *,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Return {components, total} for a candidate pair.

    `components` is the per-signal dict (values in [0, 1] or None).
    `total` is the weighted sum clipped to [0, 100].
    """
    w = weights or DEFAULT_WEIGHTS
    components: dict[str, float | None] = {}

    # Name similarity. A MISSING name is neutral (None, omitted), not 0 -- Justia
    # carries no firm name, and scoring it 0 wrongly penalized every Justia pair.
    name_a = (a.name_normalized or "").strip()
    name_b = (b.name_normalized or "").strip()
    if name_a and name_b:
        components["name_sim"] = fuzz.token_set_ratio(name_a, name_b) / 100.0
    else:
        components["name_sim"] = None

    # Phone exact-match (when both present).
    pa = (a.phone_normalized or "").strip()
    pb = (b.phone_normalized or "").strip()
    if pa and pb:
        components["phone_exact"] = 1.0 if pa == pb else 0.0
    else:
        components["phone_exact"] = None

    # Website exact-match (when both present).
    wa = (a.website_normalized or "").strip().lower()
    wb = (b.website_normalized or "").strip().lower()
    # Only IDENTITY domains count: a shared aggregator / social / platform domain
    # (facebook.com, weebly.com, ...) is not evidence of the same firm.
    if wa and wb and is_identity_website(wa) and is_identity_website(wb):
        components["website_exact"] = 1.0 if wa == wb else 0.0
    else:
        components["website_exact"] = None

    # City match (case-insensitive; both must be present).
    ca = (a.primary_city or "").strip().lower()
    cb = (b.primary_city or "").strip().lower()
    if ca and cb:
        components["city_match"] = 1.0 if ca == cb else 0.0
    else:
        components["city_match"] = None

    # State match.
    sa = (a.primary_state or "").strip().upper()
    sb = (b.primary_state or "").strip().upper()
    if sa and sb:
        components["state_match"] = 1.0 if sa == sb else 0.0
    else:
        components["state_match"] = None

    # Suffix mismatch as a small negative.
    suffix_a = _name_suffix(a)
    suffix_b = _name_suffix(b)
    if suffix_a and suffix_b and suffix_a != suffix_b:
        components["suffix_diff_penalty"] = -1.0
    elif suffix_a and suffix_b:
        components["suffix_diff_penalty"] = 0.0
    else:
        # Don't penalize when one side is missing a suffix.
        components["suffix_diff_penalty"] = 0.0

    # Total: weighted sum of present components.
    total = 0.0
    for k, weight in w.items():
        v = components.get(k)
        if v is None:
            continue
        total += weight * v

    # Strong-identifier overrides. First apply positive FLOORS (decisive
    # same-firm evidence), then CAPS for contradicting evidence -- caps are
    # applied last so they win over a floor when signals conflict.
    name_sim = components.get("name_sim")
    name_conflict = (
        name_sim is not None
        and name_sim < NAME_CONFLICT_SIM
        and is_firm_name(a.name_raw)
        and is_firm_name(b.name_raw)
    )
    # Floors.
    if components.get("website_exact") == 1.0 and not name_conflict:
        total = max(total, WEBSITE_MERGE_FLOOR)
    if (
        components.get("phone_exact") == 1.0
        and name_sim is not None
        and name_sim >= STRONG_NAME_SIM
    ):
        total = max(total, PHONE_NAME_FLOOR)
    if (
        name_sim is not None
        and name_sim >= NAME_SIM_FOR_LOCATION
        and components.get("city_match") == 1.0
        and components.get("state_match") == 1.0
    ):
        total = max(total, NAME_LOCATION_FLOOR)
    # Caps (contradicting evidence wins).
    if name_conflict:
        total = min(total, NAME_CONFLICT_CAP)
    if components.get("website_exact") == 0.0:
        total = min(total, WEBSITE_CONFLICT_CAP)

    total = max(0.0, min(100.0, total))
    return {"components": components, "total": round(total, 2)}
