"""Multi-key blocking for entity resolution.

Generates candidate match pairs (source_record_a_id < source_record_b_id)
by grouping source records on small set of blocking keys. The goal is
to make O(N^2) scoring feasible without missing real matches —
records that share AT LEAST ONE blocking key become a candidate pair
and get scored later.

Blocking keys (project-decided in the handoff):

  * `phone:<E.164>` — exact normalized phone match
  * `website:<bare-domain>` — exact normalized website match
  * `name_state:<name-prefix>|<state>` — first 8 chars of
    `name_normalized` together with `primary_state`

Each record may emit zero, one, or several keys. Records with no
phone/website/state+name end up unblockable on this pass — they'll
remain isolated until a future enrichment pass populates a
blocking-key-eligible field.

Empirically on 1,498 firms across 3 sources, this multi-key
approach generates O(10^4) candidate pairs — well under the
O(10^6) cross-product.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator

from legal_sourcing.models import FirmSourceRecord
from legal_sourcing.resolution.identity import is_identity_website

# Length of the name prefix used in the (name_prefix, state) key. Eight
# characters is short enough to survive minor suffix differences
# ("LLP" vs "P.C.") and long enough to avoid pairing every two-letter
# firm-name prefix in a state.
NAME_PREFIX_LEN = 8


def make_blocking_keys(record: FirmSourceRecord) -> set[str]:
    """Return the set of blocking keys this record contributes to.

    Each key is a string with a discriminating prefix so the same
    bucket can't be accidentally collided across key types.
    """
    keys: set[str] = set()

    phone = (record.phone_normalized or "").strip()
    if phone:
        keys.add(f"phone:{phone}")

    website = (record.website_normalized or "").strip()
    # Skip aggregator / social / website-builder domains: a shared non-identity
    # domain (facebook.com, weebly.com, ...) would bucket unrelated firms.
    if website and is_identity_website(website):
        keys.add(f"website:{website}")

    name_norm = (record.name_normalized or "").strip()
    state = (record.primary_state or "").strip().upper()
    if name_norm and state:
        prefix = name_norm[:NAME_PREFIX_LEN]
        if prefix:
            keys.add(f"name_state:{prefix}|{state}")

    return keys


def generate_candidate_pairs(
    records: Iterable[FirmSourceRecord],
) -> Iterator[tuple[int, int]]:
    """Yield candidate (smaller_id, larger_id) pairs for scoring.

    A pair is emitted once even when the two records share multiple
    blocking keys. Pairs are scoped to (a_id < b_id) to match the
    `MatchReviewQueue` check constraint.
    """
    by_key: dict[str, list[int]] = defaultdict(list)
    for r in records:
        for key in make_blocking_keys(r):
            by_key[key].append(r.id)

    seen: set[tuple[int, int]] = set()
    for ids in by_key.values():
        if len(ids) < 2:
            continue
        unique_ids = sorted(set(ids))
        for i in range(len(unique_ids)):
            for j in range(i + 1, len(unique_ids)):
                pair = (unique_ids[i], unique_ids[j])
                if pair in seen:
                    continue
                seen.add(pair)
                yield pair


def bucket_stats(records: Iterable[FirmSourceRecord]) -> dict[str, int]:
    """Diagnostic: count records per key prefix. Helps spot
    over-/under-blocking when tuning."""
    counts: dict[str, int] = defaultdict(int)
    for r in records:
        for key in make_blocking_keys(r):
            prefix = key.split(":", 1)[0]
            counts[prefix] += 1
    return dict(counts)
