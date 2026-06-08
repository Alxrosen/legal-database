"""Engine-agnostic evaluation harness for canonical firm resolution.

This is the **gate** for the Splink pivot (audit P1/P2): before we swap the
hand-rolled blocking/scoring/union-find for Splink, we need a *measured*
operating point. Both engines score the SAME labeled candidate-pair set, so the
adopt-iff-it-beats-bespoke decision is evidence-based, not eyeballed.

Design (lead-with-idiomatic, semi-supervised — no hand-labels required for the
bulk):

* **Ground truth** for a pair comes from a *near-unique identifier*: a firm's own
  **identity website** (`is_identity_website` bare domain). Two source records on
  the same identity domain are the same firm; two on *different* identity domains
  are different firms. The **known-firm oracle** (`ORACLE_FIRMS`) patches the
  exceptions identity-domain labeling can't see — multi-domain firms (Thompson &
  Hiller, Dickinson Wright) whose distinct domains are still ONE firm.
* **Hard negatives** fall out for free: records sharing a low-value blocking key
  (a shared phone, a name prefix) but with *conflicting* identity domains — e.g.
  the lead-gen / answering-service phone `+17623800028` (15 distinct firm
  domains). (NB: `+18336461198`/450 records is NOT a lead-gen case — it's Morgan
  & Morgan's own number on one domain, i.e. a correct single-firm cluster.)
* **The ambiguous middle** — candidate pairs we *cannot* auto-label (a side has
  no identity website and isn't in the oracle) — is exported as a stratified
  sample for Alex to adjudicate clerically. That is the only part automation
  can't own; everything else is reproducible from the DB.

What it measures (engine = a function ``(a_id, b_id) -> match prob``):

* **Pairwise** precision / recall / F1 across a threshold sweep, computed
  *end-to-end* (a pair the engine's blocking never proposes scores 0, so it
  counts as a recall miss — blocking gaps are penalized, not hidden).
* **B-cubed** precision / recall / F1 on the resulting clusters vs the
  identity-domain/oracle ground-truth clustering.
* **Blocking recall ceiling** — of the truth-positive pairs, how many the
  engine's blocking even proposes (the hard cap on recall).

CLI::

    # baseline the hand-rolled matcher
    uv run python -m legal_sourcing.resolution.eval_harness bespoke
    # export the un-labelable ambiguous middle for clerical review
    uv run python -m legal_sourcing.resolution.eval_harness clerical-sample --n 150
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_sourcing.db import make_engine
from legal_sourcing.models import FirmSourceRecord
from legal_sourcing.resolution.apply import _UnionFind
from legal_sourcing.resolution.blocking import generate_candidate_pairs
from legal_sourcing.resolution.identity import is_identity_website
from legal_sourcing.resolution.scoring import score_pair

# Deterministic sampling so the labeled set is reproducible across runs/engines.
_SEED = 20260608
# Cap on truth-positive pairs synthesized per firm (big firms like Morgan &
# Morgan have C(465,2)≈108k intra-firm pairs; we sample to keep the set balanced
# and the metric from being dominated by one mega-firm).
POSITIVES_PER_FIRM_CAP = 30


# ---------------------------------------------------------------------------
# Known-firm oracle (evidence-verified against the live DB, 2026-06-08)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OracleFirm:
    """A hand-curated firm whose identity domain(s) define one true cluster.

    ``domains`` may hold MORE THAN ONE bare domain — that is the whole point for
    the multi-domain regression cases (Canonizer OPEN ITEM 2): the distinct
    domains are still a single firm, which plain identity-domain labeling would
    wrongly split.
    """

    key: str
    display: str
    domains: tuple[str, ...]
    note: str = ""


# Record counts are from the live DB (see COORDINATION 2026-06-08); kept as the
# regression oracle the Splink pivot must not regress.
ORACLE_FIRMS: tuple[OracleFirm, ...] = (
    OracleFirm(
        "snell_wilmer", "Snell & Wilmer", ("swlaw.com",), "39 recs across az_bar/justia/website"
    ),
    OracleFirm(
        "morgan_morgan",
        "Morgan & Morgan",
        ("forthepeople.com",),
        "465 recs; toll-free +18336461198 is THEIR OWN number on this one domain -> correct single firm",
    ),
    OracleFirm("kutak_rock", "Kutak Rock", ("kutakrock.com",), "105 recs"),
    OracleFirm(
        "thompson_hiller",
        "Thompson & Hiller",
        ("thompsonhillerdefense.com", "grandstrandlaw.com"),
        "MULTI-DOMAIN: 2 distinct domains, one firm (shared phone + identical enrichment)",
    ),
    OracleFirm(
        "dickinson_wright",
        "Dickinson Wright",
        ("dickinson-wright.com", "dickinsonwright.com"),
        "MULTI-DOMAIN: hyphen vs no-hyphen, one firm",
    ),
)

# Lead-gen / answering-service phones VERIFIED to be shared across DISTINCT firm
# domains -> intra-group cross-domain pairs are true NEGATIVES (a blind
# phone-merge would wrongly fuse them). This is the case the toll-free guard /
# Splink's term-frequency phone adjustment must handle.
LEADGEN_PHONES: tuple[str, ...] = ("+17623800028",)


def _domain_to_oracle_firm() -> dict[str, str]:
    out: dict[str, str] = {}
    for f in ORACLE_FIRMS:
        for d in f.domains:
            out[d] = f.key
    return out


# ---------------------------------------------------------------------------
# Ground-truth labeling
# ---------------------------------------------------------------------------


def identity_domain(rec: FirmSourceRecord) -> str | None:
    """The record's bare identity domain, or None if it has no identity website
    (so it's not auto-labelable)."""
    w = (rec.website_normalized or "").strip().lower()
    if w and is_identity_website(w):
        return w
    return None


def true_firm_id(rec: FirmSourceRecord, domain_to_firm: dict[str, str]) -> str | None:
    """A record's ground-truth firm id, or None when un-labelable.

    Oracle membership wins (it merges multi-domain firms); otherwise the identity
    domain itself is the firm id (``web:<domain>``); records with no identity
    website are un-labelable here (-> clerical sample).
    """
    dom = identity_domain(rec)
    if dom is None:
        return None
    if dom in domain_to_firm:
        return f"oracle:{domain_to_firm[dom]}"
    return f"web:{dom}"


@dataclass(frozen=True)
class LabeledPair:
    a_id: int
    b_id: int
    match: int  # 1 = same firm, 0 = different
    source: str  # how the label was derived

    def key(self) -> tuple[int, int]:
        return (self.a_id, self.b_id)


def _ordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


# ---------------------------------------------------------------------------
# Build the labeled evaluation set
# ---------------------------------------------------------------------------


@dataclass
class EvalSet:
    """The labeled pairs + the ground-truth assignment for the working records.

    ``truth_by_id`` maps record id -> true firm id over the labelable working set
    (used for B-cubed). ``candidate_keys`` is the set of pairs the BESPOKE
    blocking proposes (used to compute the blocking recall ceiling and to gate
    the end-to-end pairwise score)."""

    pairs: list[LabeledPair]
    truth_by_id: dict[int, str]
    records: dict[int, FirmSourceRecord]
    candidate_keys: set[tuple[int, int]]

    def positives(self) -> list[LabeledPair]:
        return [p for p in self.pairs if p.match == 1]

    def negatives(self) -> list[LabeledPair]:
        return [p for p in self.pairs if p.match == 0]


def _labelable_records(session: Session) -> list[FirmSourceRecord]:
    """All records that carry an identity website (auto-labelable) plus the
    lead-gen phone groups (so the hard negatives are present)."""
    recs = session.scalars(
        select(FirmSourceRecord).where(
            FirmSourceRecord.website_normalized.is_not(None),
            FirmSourceRecord.website_normalized != "",
        )
    ).all()
    out = {r.id: r for r in recs if identity_domain(r) is not None}
    if LEADGEN_PHONES:
        leadgen = session.scalars(
            select(FirmSourceRecord).where(FirmSourceRecord.phone_normalized.in_(LEADGEN_PHONES))
        ).all()
        for r in leadgen:
            out.setdefault(r.id, r)
    return list(out.values())


def build_eval_set(
    session: Session,
    *,
    positives_cap: int = POSITIVES_PER_FIRM_CAP,
    seed: int = _SEED,
) -> EvalSet:
    """Construct the labeled candidate-pair set from current DB state.

    Pairs come from two places:
      1. **Candidate pairs** the bespoke blocking proposes over the labelable
         working set — labeled match/non-match by identity-domain+oracle. This is
         the precision-relevant population (the pairs an engine actually scores).
      2. **Synthesized truth-positive pairs** (a sampled spanning set per firm),
         so recall is measured even for same-firm pairs that blocking MISSED
         (those score 0 end-to-end -> counted as recall misses).
    """
    rng = random.Random(seed)
    d2f = _domain_to_oracle_firm()
    records = {r.id: r for r in _labelable_records(session)}
    truth_by_id: dict[int, str] = {}
    for rid, rec in records.items():
        fid = true_firm_id(rec, d2f)
        if fid is not None:
            truth_by_id[rid] = fid

    # firm id -> member record ids (only labelable members)
    by_firm: dict[str, list[int]] = defaultdict(list)
    for rid, fid in truth_by_id.items():
        by_firm[fid].append(rid)

    pairs: dict[tuple[int, int], LabeledPair] = {}

    # 1) Candidate pairs from bespoke blocking, labeled where both sides labelable.
    candidate_keys: set[tuple[int, int]] = set()
    for a, b in generate_candidate_pairs(records.values()):
        candidate_keys.add((a, b))
        fa, fb = truth_by_id.get(a), truth_by_id.get(b)
        if fa is None or fb is None:
            continue  # un-labelable -> clerical territory, not scored here
        match = 1 if fa == fb else 0
        src = "candidate_pos" if match else "candidate_neg"
        pairs[(a, b)] = LabeledPair(a, b, match, src)

    # 2) Synthesized truth positives (sampled spanning set per firm) to measure
    #    recall beyond what blocking proposed.
    for fid, members in by_firm.items():
        if len(members) < 2:
            continue
        members = sorted(members)
        chosen: set[tuple[int, int]] = set()
        # spanning chain guarantees connectivity coverage
        for i in range(len(members) - 1):
            chosen.add(_ordered(members[i], members[i + 1]))
            if len(chosen) >= positives_cap:
                break
        # a few random extra cross pairs
        while len(chosen) < min(positives_cap, len(members) * (len(members) - 1) // 2):
            a, b = rng.sample(members, 2)
            chosen.add(_ordered(a, b))
        for a, b in chosen:
            if (a, b) not in pairs:
                src = "oracle_pos" if fid.startswith("oracle:") else "identity_pos"
                pairs[(a, b)] = LabeledPair(a, b, 1, src)

    return EvalSet(
        pairs=list(pairs.values()),
        truth_by_id=truth_by_id,
        records=records,
        candidate_keys=candidate_keys,
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class PairwisePoint:
    threshold: float
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def pairwise_sweep(
    eval_set: EvalSet,
    score_fn: Callable[[int, int], float],
    thresholds: Iterable[float],
) -> list[PairwisePoint]:
    """Score every labeled pair once, then sweep thresholds.

    ``score_fn`` returns a match score on the SAME scale as ``thresholds`` (the
    bespoke engine returns [0, 100]). A pair the engine wouldn't propose must
    score below every threshold (the caller wires blocking into ``score_fn``)."""
    scored = [(p.match, score_fn(p.a_id, p.b_id)) for p in eval_set.pairs]
    out = []
    for th in thresholds:
        tp = fp = fn = tn = 0
        for match, s in scored:
            pred = 1 if s >= th else 0
            if match and pred:
                tp += 1
            elif match and not pred:
                fn += 1
            elif not match and pred:
                fp += 1
            else:
                tn += 1
        out.append(PairwisePoint(th, tp, fp, fn, tn))
    return out


def bcubed(true_by_id: dict[int, str], pred_by_id: dict[int, str]) -> tuple[float, float, float]:
    """B-cubed precision / recall / F1 over the records present in BOTH maps.

    For each record: precision = |same-pred ∧ same-true| / |same-pred|;
    recall = |same-pred ∧ same-true| / |same-true|. Averaged over records.
    """
    ids = [i for i in true_by_id if i in pred_by_id]
    if not ids:
        return 0.0, 0.0, 0.0
    true_clusters: dict[str, set[int]] = defaultdict(set)
    pred_clusters: dict[object, set[int]] = defaultdict(set)
    for i in ids:
        true_clusters[true_by_id[i]].add(i)
        pred_clusters[pred_by_id[i]].add(i)
    p_sum = r_sum = 0.0
    for i in ids:
        t = true_clusters[true_by_id[i]]
        c = pred_clusters[pred_by_id[i]]
        inter = len(t & c)
        p_sum += inter / len(c)
        r_sum += inter / len(t)
    n = len(ids)
    p, r = p_sum / n, r_sum / n
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f1


# ---------------------------------------------------------------------------
# Bespoke engine adapters
# ---------------------------------------------------------------------------


def bespoke_score_fn(eval_set: EvalSet) -> Callable[[int, int], float]:
    """End-to-end bespoke score: the pairwise score IF blocking proposed the
    pair, else 0 (so blocking misses count as recall misses)."""
    recs = eval_set.records
    cand = eval_set.candidate_keys

    def fn(a: int, b: int) -> float:
        if (a, b) not in cand and (b, a) not in cand:
            return 0.0
        return score_pair(recs[a], recs[b])["total"]

    return fn


def bespoke_clusters(eval_set: EvalSet, merge_threshold: float) -> dict[int, str]:
    """Run the bespoke union-find over the working set at a threshold; return
    record id -> predicted cluster id (as a string for B-cubed)."""
    recs = eval_set.records
    uf = _UnionFind()
    for rid in recs:
        uf.find(rid)
    for a, b in eval_set.candidate_keys:
        if score_pair(recs[a], recs[b])["total"] >= merge_threshold:
            uf.union(a, b)
    return {rid: f"c{uf.find(rid)}" for rid in eval_set.truth_by_id}


# ---------------------------------------------------------------------------
# Reporting / CLI
# ---------------------------------------------------------------------------


def _blocking_recall(eval_set: EvalSet) -> tuple[int, int]:
    """Of truth-positive pairs, how many bespoke blocking proposes."""
    pos = eval_set.positives()
    proposed = sum(1 for p in pos if (p.a_id, p.b_id) in eval_set.candidate_keys)
    return proposed, len(pos)


def _print_report(eval_set: EvalSet, thresholds: list[float]) -> None:
    npos = len(eval_set.positives())
    nneg = len(eval_set.negatives())
    print("\n=== EVAL SET ===")
    print(f"  labelable records : {len(eval_set.records)}")
    print(f"  ground-truth firms: {len(set(eval_set.truth_by_id.values()))}")
    print(f"  labeled pairs     : {len(eval_set.pairs)}  (+{npos} pos / -{nneg} neg)")
    by_src = Counter(p.source for p in eval_set.pairs)
    print(f"  by label source  : {dict(by_src)}")
    prop, tot = _blocking_recall(eval_set)
    print(
        f"  blocking ceiling  : {prop}/{tot} truth-positive pairs proposed "
        f"({100 * prop / tot:.1f}% recall ceiling)"
        if tot
        else "  blocking ceiling  : n/a"
    )

    score_fn = bespoke_score_fn(eval_set)
    print("\n=== BESPOKE pairwise sweep ===")
    print("  thresh   TP    FP    FN     prec    rec     F1")
    best = None
    for pt in pairwise_sweep(eval_set, score_fn, thresholds):
        print(
            f"  {pt.threshold:5.0f}  {pt.tp:5d} {pt.fp:5d} {pt.fn:5d}   "
            f"{pt.precision:.3f}  {pt.recall:.3f}  {pt.f1:.3f}"
        )
        if best is None or pt.f1 > best.f1:
            best = pt
    if best:
        print(
            f"  -> best F1={best.f1:.3f} @ threshold {best.threshold:.0f} "
            f"(prec={best.precision:.3f} rec={best.recall:.3f})"
        )

    # B-cubed at the production auto-merge threshold (85).
    pred = bespoke_clusters(eval_set, merge_threshold=85.0)
    bp, br, bf = bcubed(eval_set.truth_by_id, pred)
    print("\n=== BESPOKE B-cubed @ merge_threshold=85 ===")
    print(f"  precision={bp:.3f}  recall={br:.3f}  F1={bf:.3f}")
    _print_oracle_report(eval_set, pred)


def _print_oracle_report(eval_set: EvalSet, pred: dict[int, str]) -> None:
    """Per known-firm: did its records land in ONE predicted cluster?"""
    print("\n=== ORACLE firms (records -> predicted clusters) ===")
    members: dict[str, list[int]] = defaultdict(list)
    for rid, fid in eval_set.truth_by_id.items():
        members[fid].append(rid)
    for f in ORACLE_FIRMS:
        fid = f"oracle:{f.key}"
        ids = members.get(fid, [])
        clusters = Counter(pred.get(i) for i in ids)
        verdict = (
            "OK (1 cluster)"
            if len(clusters) == 1 and ids
            else ("no records" if not ids else f"SPLIT into {len(clusters)}")
        )
        print(f"  {f.display:22s} {len(ids):3d} recs -> {verdict}")


def _clerical_sample(session: Session, n: int, seed: int = _SEED) -> list[dict]:
    """Stratified sample of the AMBIGUOUS MIDDLE: candidate pairs the harness
    can't auto-label (a side has no identity website / not in oracle) whose
    bespoke score lands in the review band. Exported for human adjudication."""
    rng = random.Random(seed)
    recs = {r.id: r for r in session.scalars(select(FirmSourceRecord)).all()}
    d2f = _domain_to_oracle_firm()
    # Candidate pairs over the full corpus is expensive; restrict to records that
    # share a phone or website with at least one other (the blockable universe),
    # which is what blocking would consider anyway.
    blockable = [r for r in recs.values() if (r.phone_normalized or r.website_normalized)]
    rows: list[dict] = []
    for a, b in generate_candidate_pairs(blockable):
        ra, rb = recs[a], recs[b]
        fa = true_firm_id(ra, d2f)
        fb = true_firm_id(rb, d2f)
        if fa is not None and fb is not None:
            continue  # auto-labelable -> not the ambiguous middle
        total = score_pair(ra, rb)["total"]
        if 55 <= total <= 88:  # the band where match is genuinely uncertain
            rows.append(
                {
                    "a_id": a,
                    "b_id": b,
                    "bespoke_score": total,
                    "a_name": ra.name_raw or "",
                    "b_name": rb.name_raw or "",
                    "a_source": ra.source,
                    "b_source": rb.source,
                    "a_web": ra.website_normalized or "",
                    "b_web": rb.website_normalized or "",
                    "a_phone": ra.phone_normalized or "",
                    "b_phone": rb.phone_normalized or "",
                    "a_city": ra.primary_city or "",
                    "b_city": rb.primary_city or "",
                    "clerical_match": "",  # to be filled by reviewer: 1 / 0
                }
            )
    rng.shuffle(rows)
    # stratify by score band so the sample spans the full uncertain range
    rows.sort(key=lambda d: d["bespoke_score"])
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_bespoke = sub.add_parser("bespoke", help="Baseline the hand-rolled matcher on the eval set.")
    p_bespoke.add_argument(
        "--thresholds",
        default="40,50,55,60,70,80,85,88,90,95",
        help="Comma-separated score thresholds to sweep.",
    )
    p_cler = sub.add_parser(
        "clerical-sample", help="Export the ambiguous middle for human adjudication."
    )
    p_cler.add_argument("--n", type=int, default=150)
    p_cler.add_argument("--out", default="data/eval/clerical_sample.csv")
    args = ap.parse_args()

    engine = make_engine()
    with Session(engine) as session:
        if args.cmd == "bespoke":
            eval_set = build_eval_set(session)
            thresholds = [float(x) for x in args.thresholds.split(",")]
            _print_report(eval_set, thresholds)
        elif args.cmd == "clerical-sample":
            rows = _clerical_sample(session, args.n)
            import os

            os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
            if rows:
                with open(args.out, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                    writer.writeheader()
                    writer.writerows(rows)
            print(f"wrote {len(rows)} ambiguous-middle pairs -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
