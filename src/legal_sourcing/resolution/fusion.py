"""Truth-discovery / data-fusion canonicalization for a firm cluster.

Given a cluster of ``FirmSourceRecord`` rows (one connected component from
``resolution.apply``'s union-find) plus an optional ``WebsiteEnrichment``,
pick the canonical value for each scalar ``Firm`` field.

Why not simple source precedence (the previous ``apply._pick``)? Real clusters
break it (verified against the live DB):

  * **forthepeople.com** (Morgan & Morgan): 450 FindLaw records, most named
    after individual attorneys ("Ike Gulas"), only ~62 named "Morgan & Morgan".
    Precedence that takes the first FindLaw record per source can pick a random
    attorney's name as the firm name.
  * **swlaw.com** (Snell & Wilmer): 23 Justia records have an EMPTY name; az_bar
    supplies six spellings ("Snell & Wilmer LLP" x8, "...L.L.P." x2, ...) plus
    one mis-attributed "Arizona Supreme Court".
  * **attorney_count**: a *verified* website headcount ("500+") must beat the
    ~30 distinct attorneys we happened to scrape.

So we use field-by-field SURVIVORSHIP (the MDM golden-record model) where the
winner is a reliability- and recency-weighted vote across the cluster (the core
of truth discovery / data-fusion CONFLICT-RESOLVE), with field-specific rules:

  * ``name``  -- gate to firm-like names, group near-duplicate variants by fuzzy
    similarity of the normalized form, weighted-vote the groups, and emit the
    most-supported raw surface form of the winning group (a dedupe-style
    centroid). A lone outlier ("Arizona Supreme Court") lands in its own
    low-weight group and loses.
  * ``phone`` / ``website`` -- weighted vote on the exact normalized value.
    Aggregator / social domains (``is_aggregator_domain``) are never a firm
    website.
  * ``attorney_count`` -- a VERIFIED website headcount is authoritative;
    otherwise the distinct-attorney union across the cluster, flagged as a
    minimum (we only see the attorneys we scraped).
  * ``year_founded`` -- weighted vote of any source-supplied years; else derived
    from a verified website's years-in-operation (approximate).

Each pick records provenance (source record, method, support, confidence, and
field-specific flags such as ``is_min``) into ``Firm.field_provenance`` -- a
free-form JSON column, so this needs no schema change.

References: TruthFinder (Yin, Han & Yu, 2007); data-fusion conflict resolution
(Bleiholder & Naumann, 2008); MDM survivorship / golden record; the
``dedupe`` library's centroid ``canonicalize``. Design of record:
docs/assumptions.md 2026-06-03 (iterative truth discovery) and 2026-06-04.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rapidfuzz import fuzz

from legal_sourcing.models import FirmSourceRecord, WebsiteEnrichment
from legal_sourcing.resolution.identity import is_firm_name, is_identity_website

# ---------------------------------------------------------------------------
# Source reliability + recency (the truth-discovery weighting)

# Per-origin reliability priors. Frequency + completeness usually dominate the
# outcome; these weight multi-source votes and break ties. Justia is low
# because it carries no firm name and thin firm metadata -- but it still
# contributes website / phone / attorney claims. Tunable; a future global
# TruthFinder pass could *learn* these from inter-source agreement.
SOURCE_RELIABILITY: dict[str, float] = {
    "martindale": 1.0,
    "findlaw": 0.9,
    "az_bar": 0.85,
    "justia": 0.6,
}
DEFAULT_RELIABILITY = 0.5
# A Martindale firm-profile-enriched record (clean firm-level fields) gets a
# small bump over a bare directory record from the same source.
ENRICHED_BONUS = 0.10
# Recency half-life: a claim's weight halves every N days. Gentle on purpose --
# the whole corpus is scraped within weeks, so recency is a mild tiebreaker that
# favours re-fetched / updated records, never the dominant signal.
RECENCY_HALF_LIFE_DAYS = 180.0
# Fuzzy-merge name variants whose normalized forms score at/above this
# (token_set_ratio). Safe to be aggressive: the cluster is already one firm, so
# this only chooses among variants of the same name.
NAME_GROUP_THRESHOLD = 88.0
# Don't derive a founding year from a website's "years in operation" below this:
# small values are noisy / often mis-extracted, and with the "N+ years" (is_min)
# semantics they'd yield a near-current, misleading founding year (seen on
# denisekirbylaw.com: "1 year" -> "founded 2025").
MIN_YEARS_FOR_FOUNDING_DERIVATION = 5


def source_reliability(rec: FirmSourceRecord) -> float:
    """Reliability prior for the record's source (with an enrichment bonus)."""
    base = SOURCE_RELIABILITY.get(rec.source, DEFAULT_RELIABILITY)
    if getattr(rec, "enrichment_status", None) == "enriched":
        base += ENRICHED_BONUS
    return base


def recency_weight(
    ts: datetime | None, now: datetime, half_life_days: float = RECENCY_HALF_LIFE_DAYS
) -> float:
    """Exponential recency decay in (0, 1]. Unknown timestamp -> mild 0.7."""
    if ts is None:
        return 0.7
    if ts.tzinfo is None:  # sqlite hands back naive datetimes; treat as UTC
        ts = ts.replace(tzinfo=UTC)
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life_days)


def claim_weight(rec: FirmSourceRecord, now: datetime) -> float:
    """Weight of a single record's claim: reliability x recency."""
    return source_reliability(rec) * recency_weight(getattr(rec, "scraped_at", None), now)


# ---------------------------------------------------------------------------
# Field choice + generic weighted vote


@dataclass
class FieldChoice:
    """One canonical field pick plus the provenance to explain it."""

    value: Any
    source_record_id: int | None = None
    source: str | None = None
    method: str = "weighted_vote"
    support: float = 0.0  # summed weight behind the winning value
    total_weight: float = 0.0  # summed weight of all claims for the field
    confidence: float = 0.0  # support / total_weight
    extra: dict[str, Any] = field(default_factory=dict)


def _exact_weighted_vote(
    claims: list[tuple[Any, FirmSourceRecord]],
    now: datetime,
    *,
    method: str = "weighted_vote",
    raw_of=None,
) -> FieldChoice | None:
    """Weighted vote on exact-equal values. `claims` are (value, record) with
    value already filtered non-empty. Winner = highest summed weight, ties
    broken by support count then value (deterministic)."""
    if not claims:
        return None
    weights: dict[Any, float] = defaultdict(float)
    recs: dict[Any, list[tuple[FirmSourceRecord, float]]] = defaultdict(list)
    total = 0.0
    for value, rec in claims:
        w = claim_weight(rec, now)
        weights[value] += w
        recs[value].append((rec, w))
        total += w
    winner = max(weights, key=lambda k: (weights[k], len(recs[k]), str(k)))
    best_rec = max(recs[winner], key=lambda rw: rw[1])[0]
    extra = {"raw": raw_of(best_rec)} if raw_of else {}
    return FieldChoice(
        value=winner,
        source_record_id=best_rec.id,
        source=best_rec.source,
        method=method,
        support=weights[winner],
        total_weight=total,
        confidence=weights[winner] / total if total else 0.0,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Attorney identity + distinct-count (moved from apply.py; re-exported there so
# existing tests keep importing them from resolution.apply).


def _attorney_identity(contact: dict[str, Any], source: str) -> str | None:
    """Dedup key for one attorney: email (globally unique) -> normalized name
    (the cross-source workhorse) -> source-scoped bar/profile id (only when
    there's no name). Cross-source dedup is name-based and imperfect (no
    universal attorney ID); email upgrades it, id is a fallback.
    """
    email = (contact.get("email_normalized") or contact.get("email_raw") or "").strip().lower()
    if email:
        return f"email:{email}"
    name = (contact.get("name_normalized") or contact.get("name_raw") or "").strip().lower()
    if name:
        return f"name:{name}"
    cid = (
        contact.get("entity_number")
        or contact.get("bar_number")
        or contact.get("source_attorney_id")
    )
    if cid:
        return f"id:{source}:{cid}"
    return None


def _aggregate_attorney_count(members: list[FirmSourceRecord]) -> int | None:
    """Count DISTINCT attorneys across the cluster (NOT MAX, NOT SUM).

    Each directory record carries its attorney(s) in `contacts`
    (az_bar / justia / martindale); FindLaw lists some individual attorneys as
    firm-level cards with empty `contacts` and the person in `name_raw`. We
    union both, deduped by `_attorney_identity`. This is a LOWER BOUND -- we
    only see the attorneys we scraped -- so callers flag it as a minimum. A
    verified website headcount, when available, supersedes it.
    """
    ids: set[str] = set()
    for m in members:
        contacts = m.contacts or []
        if contacts:
            for c in contacts:
                key = _attorney_identity(c, m.source)
                if key:
                    ids.add(key)
        elif not is_firm_name(m.name_raw):
            # Person-level record with no contacts (FindLaw per-attorney card):
            # count the named individual as one attorney.
            nm = (m.name_normalized or m.name_raw or "").strip().lower()
            if nm:
                ids.add(f"name:{nm}")
    return len(ids) or None


# ---------------------------------------------------------------------------
# Per-field fusion


def fuse_name(members: list[FirmSourceRecord], now: datetime) -> FieldChoice | None:
    """Canonical firm name: weighted vote over fuzzy-grouped firm-like variants.

    Gates out empty names (Justia) and person names (FindLaw attorney cards) via
    `looks_like_firm`. If NO firm-like name exists (e.g. a solo practitioner's
    own name), falls back to a weighted vote over all non-empty names so the
    person's name becomes the firm identity.
    """
    firm_claims = [
        (m.name_raw, m.name_normalized or "", m)
        for m in members
        if m.name_raw and is_firm_name(m.name_raw)
    ]
    method = "weighted_vote"
    if not firm_claims:
        # Fallback: any non-empty name (covers solo practitioners).
        firm_claims = [(m.name_raw, m.name_normalized or "", m) for m in members if m.name_raw]
        method = "person_fallback"
    if not firm_claims:
        return None

    # Greedy fuzzy grouping of variants by normalized form.
    groups: list[dict[str, Any]] = []
    for raw, norm, rec in firm_claims:
        w = claim_weight(rec, now)
        placed = False
        for g in groups:
            if fuzz.token_set_ratio(norm, g["norm"]) >= NAME_GROUP_THRESHOLD:
                g["weight"] += w
                g["raws"][raw] += w
                g["members"].append((raw, rec, w))
                placed = True
                break
        if not placed:
            groups.append(
                {
                    "norm": norm,
                    "weight": w,
                    "raws": Counter({raw: w}),
                    "members": [(raw, rec, w)],
                }
            )

    total = sum(g["weight"] for g in groups)
    winner = max(groups, key=lambda g: (g["weight"], len(g["members"])))
    # Representative surface form: highest accumulated weight in the group.
    surface = max(winner["raws"].items(), key=lambda kv: (kv[1], len(kv[0])))[0]
    prov_rec = max((m for m in winner["members"] if m[0] == surface), key=lambda m: m[2])[1]
    return FieldChoice(
        value=surface,
        source_record_id=prov_rec.id,
        source=prov_rec.source,
        method=method,
        support=winner["weight"],
        total_weight=total,
        confidence=winner["weight"] / total if total else 0.0,
        extra={
            "normalized": prov_rec.name_normalized,
            "variants": len(winner["raws"]),
            "groups": len(groups),
        },
    )


def fuse_phone(members: list[FirmSourceRecord], now: datetime) -> FieldChoice | None:
    """Canonical phone: weighted vote on the normalized (E.164) value. For
    multi-office firms this surfaces the most-claimed line (often the national /
    HQ number), which is acceptable for sourcing."""
    claims = [(m.phone_normalized, m) for m in members if m.phone_normalized]
    return _exact_weighted_vote(claims, now, raw_of=lambda r: r.phone_raw)


def fuse_website(members: list[FirmSourceRecord], now: datetime) -> FieldChoice | None:
    """Canonical website: weighted vote on the bare-domain value, excluding
    aggregator / social domains (which would identify a platform, not a firm)."""
    claims = [
        (m.website_normalized, m)
        for m in members
        if m.website_normalized and is_identity_website(m.website_normalized)
    ]
    return _exact_weighted_vote(claims, now, raw_of=lambda r: r.website_raw)


def fuse_attorney_count(
    members: list[FirmSourceRecord],
    enrichment: WebsiteEnrichment | None,
    now: datetime,
) -> FieldChoice | None:
    """Canonical headcount.

    A VERIFIED website headcount is authoritative -- the firm's own statement --
    UNLESS it is below the distinct attorneys we actually scraped, which means
    the site extraction under-counted (seen on hensleylegal.com, which parsed
    "1" for a 31-attorney cluster). The website count and the cluster union are
    BOTH lower bounds, so take the larger. With no verified site count, fall
    back to the union alone.
    """
    union = _aggregate_attorney_count(members)
    verified: int | None = None
    if (
        enrichment is not None
        and enrichment.url_verification_status == "verified"
        and enrichment.attorney_count_min is not None
    ):
        verified = int(enrichment.attorney_count_min)

    if verified is not None and (union is None or verified >= union):
        return FieldChoice(
            value=verified,
            source_record_id=None,
            source="website_enrichment",
            method="website_verified",
            support=1.0,
            total_weight=1.0,
            confidence=0.95,
            extra={
                "is_min": bool(enrichment.attorney_count_is_min),
                "website": enrichment.website,
                "raw": enrichment.attorney_count_raw,
            },
        )
    if union is not None and verified is not None:
        # union > verified: the website headcount under-counted; the distinct
        # attorneys we actually scraped are the tighter lower bound.
        return FieldChoice(
            value=union,
            source_record_id=None,
            source="cluster_union",
            method="union_over_website",
            support=float(union),
            total_weight=float(union),
            confidence=0.5,
            extra={"is_min": True, "website_min": verified, "website": enrichment.website},
        )
    if union is not None:
        return FieldChoice(
            value=union,
            source_record_id=None,
            source="cluster_union",
            method="distinct_union",
            support=float(union),
            total_weight=float(union),
            confidence=0.4,
            extra={"is_min": True},
        )
    return None


def fuse_year_founded(
    members: list[FirmSourceRecord],
    enrichment: WebsiteEnrichment | None,
    now: datetime,
) -> FieldChoice | None:
    """Canonical founding year: weighted vote of source-supplied years; else
    derived (approximate) from a verified website's years-in-operation."""
    claims = [(m.year_founded, m) for m in members if m.year_founded]
    if claims:
        return _exact_weighted_vote(claims, now)
    if (
        enrichment is not None
        and enrichment.url_verification_status == "verified"
        and enrichment.years_in_operation_min
        and enrichment.years_in_operation_min >= MIN_YEARS_FOR_FOUNDING_DERIVATION
    ):
        derived = now.year - int(enrichment.years_in_operation_min)
        return FieldChoice(
            value=derived,
            source_record_id=None,
            source="website_enrichment",
            method="website_years_derived",
            support=1.0,
            total_weight=1.0,
            confidence=0.5,
            extra={
                "approximate": True,
                "years_in_operation_min": enrichment.years_in_operation_min,
                # years_is_min => "at least N years" => founded no later than derived
                "founded_no_later_than": bool(enrichment.years_is_min),
            },
        )
    return None


def fuse_states(members: list[FirmSourceRecord]) -> list[str] | None:
    """ALL distinct states across the cluster's offices (sorted) — a firm spans
    many offices, so we keep every state, not just the dominant one."""
    states = {(getattr(m, "primary_state", None) or "").strip().upper() for m in members}
    states.discard("")
    return sorted(states) or None


def fuse_cities(members: list[FirmSourceRecord]) -> list[str] | None:
    """ALL distinct office cities across the cluster (sorted). Skips values that
    look like a mis-parsed street address (contain a digit) — a known Martindale
    office-parser issue (flagged to @Fixer) — so the list stays clean city names."""
    cities = set()
    for m in members:
        c = (getattr(m, "primary_city", None) or "").strip()
        if c and not any(ch.isdigit() for ch in c):
            cities.add(c)
    return sorted(cities) or None


def fuse_practice_areas(
    members: list[FirmSourceRecord], enrichment: WebsiteEnrichment | None
) -> list[str] | None:
    """Canonical practice areas: the UNION of canonical PracticeArea slugs across
    the cluster (members' ``practice_areas_matched`` + the website enrichment's
    ``practice_areas``). Practice areas are additive — a firm covers all of them —
    so we union rather than vote."""
    areas: set[str] = set()
    for m in members:
        for p in getattr(m, "practice_areas_matched", None) or []:
            if p:
                areas.add(p)
    if enrichment is not None:
        for p in getattr(enrichment, "practice_areas", None) or []:
            if p:
                areas.add(p)
    return sorted(areas) or None


# ---------------------------------------------------------------------------
# Cluster-level fusion


@dataclass
class FusionResult:
    """The fused canonical firm (values + per-field provenance + notes)."""

    name: str
    name_normalized: str | None
    website: str | None
    website_normalized: str | None
    phone: str | None
    phone_normalized: str | None
    year_founded: int | None
    attorney_count: int | None
    city: list[str] | None
    state: list[str] | None
    practice_areas: list[str] | None
    field_provenance: dict[str, Any]
    member_ids: list[int]
    notes: list[str]


def _provenance_entry(choice: FieldChoice, now: datetime) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "value": choice.value,
        "source_record_id": choice.source_record_id,
        "source": choice.source,
        "method": choice.method,
        "support": round(choice.support, 3),
        "confidence": round(choice.confidence, 3),
        "written_at": now.isoformat(),
    }
    entry.update(choice.extra)
    return entry


def fuse_cluster(
    members: list[FirmSourceRecord],
    enrichment: WebsiteEnrichment | None = None,
    now: datetime | None = None,
) -> FusionResult:
    """Fuse one cluster of source records into a canonical firm."""
    now = now or datetime.now(UTC)

    name_c = fuse_name(members, now)
    phone_c = fuse_phone(members, now)
    website_c = fuse_website(members, now)
    acount_c = fuse_attorney_count(members, enrichment, now)
    year_c = fuse_year_founded(members, enrichment, now)
    cities = fuse_cities(members)
    states = fuse_states(members)
    practice_areas = fuse_practice_areas(members, enrichment)

    provenance: dict[str, Any] = {}
    notes: list[str] = []
    for field_name, choice in (
        ("name", name_c),
        ("phone", phone_c),
        ("website", website_c),
        ("attorney_count", acount_c),
        ("year_founded", year_c),
    ):
        if choice is not None:
            provenance[field_name] = _provenance_entry(choice, now)
    for field_name, vals in (
        ("city", cities),
        ("state", states),
        ("practice_areas", practice_areas),
    ):
        if vals:
            provenance[field_name] = {
                "value": vals,
                "source": "cluster_union",
                "method": "union",
                "count": len(vals),
                "written_at": now.isoformat(),
            }

    if acount_c is not None and acount_c.extra.get("is_min"):
        notes.append("attorney_count is a lower bound (distinct attorneys seen)")
    if name_c is not None and name_c.method == "person_fallback":
        notes.append("name fell back to a person name (no firm-like name in cluster)")
    if name_c is None:
        notes.append("no firm name available in cluster")

    return FusionResult(
        name=name_c.value if name_c else "",
        name_normalized=(name_c.extra.get("normalized") if name_c else None),
        website=(website_c.extra.get("raw") if website_c else None),
        website_normalized=(website_c.value if website_c else None),
        phone=(phone_c.extra.get("raw") if phone_c else None),
        phone_normalized=(phone_c.value if phone_c else None),
        year_founded=(year_c.value if year_c else None),
        attorney_count=(acount_c.value if acount_c else None),
        city=cities,
        state=states,
        practice_areas=practice_areas,
        field_provenance=provenance,
        member_ids=[m.id for m in members],
        notes=notes,
    )
