"""Website MUST-LINK recall pass — recover false-negative under-merges.

These are pairs that SHOULD have merged but didn't: a firm's records share its
OWN identity domain, but they split because their NAMES differ (acronym /
domain-stub / page-title / rebrand), so Splink never scored the pair above
threshold. The backstop (``dry_run.backstop_kept_edges``) can only FILTER edges
Splink proposed — it cannot create one Splink never scored — so these need edges
ADDED. (Verification of 24 sampled shared-website splits: 20/24 were real false
negatives; the only correct splits were mis-attributed domains and shared
platform/gov domains — exactly the two cases the guards below handle.)

The pass links records that share a NON-GENERIC identity domain, with two guards:

* **Gate A — generic / platform / gov domains** are skipped: a domain on more than
  ``generic_min_distinct`` distinct firm name-cores is a directory / bar / gov /
  marketing host (azbar.org, irs.gov), not one firm's identity.
* **Gate B — mis-attributed website** is excluded directionally. The
  ``source="website"`` record was built BY crawling the domain, so it is the
  domain's ground truth and is never dropped. A NON-website record is excluded
  only when NOTHING connects it to the domain: it does not share the domain's
  dominant identity (the most common name-core), has **no name<->domain affinity**
  (the domain does not encode its name), and shares no phone with the group.
  "Hagens Berman Sobol Shapiro" owns ``hbss``law.com and "Koeller Nebeker Carlson
  Haluck" owns ``knch``law.com (initials), so they merge; "Maxwell & Morgan" has
  zero affinity with ``zellaw.com`` (Zelms Erlich Lenkov's site), so it is excluded.

Redirect-aware: a record's canonical domain is the redirect TARGET when its domain
redirects cross-domain (``shermanhoward.com`` -> ``taftlaw.com`` after the
acquisition), so the acquired firm merges with the acquirer. Affinity is checked
against the record's OWN (pre-redirect) domain, so the acquired firm — whose name
encodes the old domain — is still recognised as legitimate. The redirect map is
built from the website crawler's ``resolved_url`` (and, once populated,
``WebsiteEnrichment.redirect_domain`` — see pipelines/resolve_redirects.py).
"""

from __future__ import annotations

from collections import Counter, defaultdict

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_sourcing.models import WebsiteEnrichment
from legal_sourcing.normalize.firm_name import firm_name_core
from legal_sourcing.normalize.url import normalize_url
from legal_sourcing.resolution.identity import is_identity_website

# Trailing words stripped off a domain's leading label before testing affinity, so
# "hbsslaw" -> "hbss", "cluffinjurylawyers" -> "cluff". Longest-first so "lawyers"
# is tried before "law".
_DOMAIN_STOPWORDS = (
    "lawyers",
    "lawyer",
    "attorneys",
    "attorney",
    "injury",
    "associates",
    "offices",
    "office",
    "counsel",
    "legal",
    "firm",
    "group",
    "law",
    "pllc",
    "lllp",
    "llp",
    "llc",
    "plc",
    "pc",
    "pa",
    "co",
)


def build_redirect_map(session: Session) -> dict[str, str]:
    """``{domain -> cross-domain redirect target}`` from the website crawler's
    ``resolved_url``. Only cross-domain redirects to another IDENTITY domain are
    kept (same-domain, and redirects into a platform/aggregator, are omitted so
    ``canon`` falls back to the record's own domain)."""
    out: dict[str, str] = {}
    for website, resolved_url in session.execute(
        select(WebsiteEnrichment.website, WebsiteEnrichment.resolved_url)
    ):
        if not website or not resolved_url:
            continue
        src = normalize_url(website)
        tgt = normalize_url(resolved_url)
        if src and tgt and tgt != src and is_identity_website(tgt):
            out[src] = tgt
    return out


def _name_domain_affinity(name: str | None, domain: str | None) -> bool:
    """True when ``domain`` plausibly ENCODES ``name`` — a distinctive name token
    is in the domain's leading label, or the label matches the name's initials.
    This is what tells a firm's own (acronym/stub) domain from a mis-attributed
    one: "hagens berman sobol shapiro" -> ``hbss``; "maxwell & morgan" has nothing
    to do with ``zellaw``."""
    if not isinstance(name, str) or not name or not isinstance(domain, str) or not domain:
        return False
    toks = [t for t in firm_name_core(name) if t]
    if not toks:
        return False
    label = domain.split(".")[0].replace("-", "").replace("_", "")
    changed = True
    while changed:
        changed = False
        for w in _DOMAIN_STOPWORDS:
            if len(label) > len(w) and label.endswith(w):
                label = label[: -len(w)]
                changed = True
                break
    if not label:
        return False
    if any(len(t) >= 3 and t in label for t in toks):
        return True
    initials = "".join(t[0] for t in toks)
    return len(initials) >= 2 and (label.startswith(initials) or initials.startswith(label))


def _is_misattribution_outlier(
    i: int,
    ids: list[int],
    src_of: dict,
    core_of: dict,
    phone_of: dict,
    affinity_of: dict,
    dominant: str | None,
) -> bool:
    """True when record ``i``'s website looks mis-attributed to this domain (so it
    must NOT merge into the domain's firm). A record is excluded only when NOTHING
    connects it to the domain: it is not the crawled website record, does not share
    the domain's dominant identity, has no name<->domain affinity, and shares no
    phone with the group. Anything corroborated defaults to MERGE (favor recall)."""
    if src_of.get(i) == "website":
        return False  # built BY crawling the domain -> ground truth, never excluded
    ci = core_of.get(i)
    if not isinstance(ci, str) or not ci or ci == dominant:
        return False  # shares the domain's dominant identity (or is nameless)
    if affinity_of.get(i):
        return False  # the domain encodes this firm's name -> it is really theirs
    pi = phone_of.get(i)
    others_ph = {
        phone_of.get(j)
        for j in ids
        if j != i and isinstance(phone_of.get(j), str) and phone_of.get(j)
    }
    shares_phone = bool(pi) and bool(others_ph) and pi in others_ph
    # not the owner, name unrelated to the domain, no shared phone -> mis-attribution
    return not shares_phone


def website_must_link_edges(
    df: pd.DataFrame,
    redirect_map: dict[str, str],
    *,
    generic_min_distinct: int = 5,
) -> tuple[list[tuple[int, int]], dict, dict[str, dict]]:
    """Build must-link edges for shared-identity-domain false negatives.

    Returns ``(edges, stats, groups)``:
      * ``edges`` — star edges (anchor, member) to union each domain's kept records
      * ``stats`` — counters (domains_linked, edges, records_linked, generic_domains_skipped, misattribution_excluded)
      * ``groups`` — per NON-GENERIC domain: ``{members, kept, excluded}`` (for QA)
    """
    dom_of = dict(zip(df["unique_id"], df["website_identity"], strict=False))
    core_of = dict(zip(df["unique_id"], df["name_core_key"], strict=False))
    src_of = dict(zip(df["unique_id"], df["source"], strict=False))
    name_of = dict(zip(df["unique_id"], df["name_normalized"], strict=False))
    phone_of = dict(zip(df["unique_id"], df["phone_normalized"], strict=False))

    # Canonical (redirect-aware) identity domain per record; affinity is tested
    # against the record's OWN (pre-redirect) domain.
    canon: dict[int, str] = {}
    affinity_of: dict[int, bool] = {}
    for i_raw, d in dom_of.items():
        if not isinstance(d, str) or not d:
            continue
        c = redirect_map.get(d, d)
        if c and is_identity_website(c):
            i = int(i_raw)
            canon[i] = c
            affinity_of[i] = _name_domain_affinity(name_of.get(i_raw), d)

    by_dom: dict[str, list[int]] = defaultdict(list)
    for i, c in canon.items():
        by_dom[c].append(i)

    # The domain's main identity ("dominant" core): the firm whose name the domain
    # ENCODES (has affinity) owns it; ties / none fall back to the most common
    # name-core. Affinity — not a head count — picks the owner so a mis-attributor
    # that ties on record count (GlassRatner on rlb.com) cannot pose as the owner.
    owner_core: dict[str, str] = {}
    for dom, ids in by_dom.items():
        distinct = {core_of.get(i) for i in ids if isinstance(core_of.get(i), str)}
        if not distinct or len(distinct) > generic_min_distinct:
            continue
        aff = Counter(
            core_of.get(i)
            for i in ids
            if affinity_of.get(i) and isinstance(core_of.get(i), str) and core_of.get(i)
        )
        allc = Counter(
            core_of.get(i) for i in ids if isinstance(core_of.get(i), str) and core_of.get(i)
        )
        chosen = aff or allc
        if chosen:
            owner_core[dom] = chosen.most_common(1)[0][0]

    edges: list[tuple[int, int]] = []
    stats: Counter = Counter()
    groups: dict[str, dict] = {}
    for dom, ids in by_dom.items():
        if len(ids) < 2:
            continue
        distinct_cores = {core_of.get(i) for i in ids if isinstance(core_of.get(i), str)}
        if len(distinct_cores) > generic_min_distinct:
            stats["generic_domains_skipped"] += 1
            continue
        dominant = owner_core.get(dom)
        kept, excluded = [], []
        for i in ids:
            if _is_misattribution_outlier(i, ids, src_of, core_of, phone_of, affinity_of, dominant):
                excluded.append(i)
                stats["misattribution_excluded"] += 1
            else:
                kept.append(i)
        groups[dom] = {"members": ids, "kept": kept, "excluded": excluded}
        if len(kept) >= 2:
            anchor = min(kept)
            edges.extend((anchor, i) for i in kept if i != anchor)
            stats["domains_linked"] += 1
            stats["records_linked"] += len(kept)
    stats["edges"] = len(edges)
    return edges, dict(stats), groups
