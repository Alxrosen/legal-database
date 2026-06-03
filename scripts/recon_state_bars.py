"""State-bar reconnaissance — triage Tier-1+2 endpoints before building configs.

The scrapability claims in docs/data_sources/state_bars.md come from one-shot
WebFetches and are partly unverified. This script probes each listed URL LIVE
with realistic browser headers and characterizes the landing page so we know,
per state, whether it is:

  * server_form   — server-rendered HTML with a real search form (scrapable)
  * js_shell      — empty SPA shell, needs headless (push to Tier 3)
  * captcha       — recaptcha/hcaptcha gate
  * cloudflare    — CF / WAF challenge
  * referral      — redirects to a referral/aggregator service (skip per doc)
  * error         — non-2xx / unreachable

It is a FIRST pass: a bare GET of the landing page. Form-param / result-row
detail comes after, per green state. Polite: sequential, ~2s between hosts.

Outputs:
  * raw gzipped HTML under data/raw/state_bars/{date}/recon/{abbr}.html.gz
    + a {abbr}.json sidecar with the characterization
  * a printed verdict table + a summary JSON at
    data/raw/state_bars/{date}/recon/_summary.json

Usage:
    uv run python scripts/recon_state_bars.py
    uv run python scripts/recon_state_bars.py --only wy,fl,wi
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
from selectolax.parser import HTMLParser  # noqa: E402

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Domains that mean "this is a referral / aggregator, not the real bar roll".
REFERRAL_HOSTS = (
    "martindale.com",
    "findlaw.com",
    "avvo.com",
    "justia.com",
    "lawyers.com",
    "superlawyers.com",
    "reliaguide.com",
    "legalmatch.com",
)
CAPTCHA_MARKERS = (
    "recaptcha",
    "g-recaptcha",
    "hcaptcha",
    "h-captcha",
    "i'm not a robot",
    "verify you are a human",
    "verify you are not a robot",
)
CF_MARKERS = (
    "just a moment",
    "cf-mitigated",
    "attention required",
    "cf_chl_",
    "checking your browser",
    "_incapsula_",
    "incident id",
)


@dataclass
class BarTarget:
    abbr: str
    name: str
    tier: int
    url: str
    note: str = ""
    requires_param: bool = False  # search needs a name / param to return rows


# Tier 1 + Tier 2 from docs/data_sources/state_bars.md (mandatory-roll URLs).
TARGETS: list[BarTarget] = [
    # ---- Tier 1: easy, server-rendered, firm field exposed ----
    BarTarget(
        "wy",
        "Wyoming",
        1,
        "https://www.wyomingbar.org/for-the-public/hire-a-lawyer/lawyer-search/",
        "explicit Firm/Organization field; WordPress; ~3k",
    ),
    BarTarget(
        "wi",
        "Wisconsin",
        1,
        "https://www.wisbar.org/Pages/BasicLawyerSearch.aspx",
        "SharePoint form; ~24k; advanced keyword covers firm",
        requires_param=True,
    ),
    BarTarget(
        "id",
        "Idaho",
        1,
        "https://isb.idaho.gov/licensing-mcle/attorney-roster-search/",
        "data at apps.isb.idaho.gov/licensing/attorney_roster.cfm; last-name prefix",
        requires_param=True,
    ),
    BarTarget(
        "ks",
        "Kansas",
        1,
        "https://directory-kard.kscourts.gov/",
        "Supreme Court mandatory; any-part-of-name",
        requires_param=True,
    ),
    BarTarget(
        "nd",
        "North Dakota",
        1,
        "https://www.ndcourts.gov/lawyers",
        "Judicial branch; searchable by initial/city/state; ~2.9k",
    ),
    BarTarget(
        "ky",
        "Kentucky",
        1,
        "https://kybar.org/cv5/cgi-bin/utilities.dll/openpage?WRP=LawyerLocator.htm",
        "mandatory ~19.5k; optional fields",
    ),
    BarTarget(
        "mt",
        "Montana",
        1,
        "https://www.montanabar.org/cv5/cgi-bin/utilities.dll/openpage?WRP=membersearch.htm",
        "simple GET form; firm in profile",
    ),
    BarTarget(
        "or",
        "Oregon",
        1,
        "https://www.osbar.org/members/membersearch_start.asp",
        "simple public search; firm on profile",
        requires_param=True,
    ),
    BarTarget(
        "nc",
        "North Carolina",
        1,
        "https://portal.ncbar.gov/verification/search.aspx",
        "server-rendered; max 250/query; needs 2 chars of last name",
        requires_param=True,
    ),
    BarTarget(
        "fl",
        "Florida",
        1,
        "https://www.floridabar.org/directories/find-mbr/",
        "mandatory ~110k; rich form; no captcha visible",
        requires_param=True,
    ),
    BarTarget(
        "il",
        "Illinois",
        1,
        "https://iardc.org/Lawyer/Search",
        "ARDC mandatory ~90k; needs last name",
        requires_param=True,
    ),
    BarTarget(
        "nm",
        "New Mexico",
        1,
        "https://www.sbnm.org/For-Public/I-Need-a-Lawyer/Online-Bar-Directory",
        "mandatory; search by name/bar#",
        requires_param=True,
    ),
    BarTarget(
        "ia",
        "Iowa",
        1,
        "https://www.iacourtcommissions.org/ords/f?p=106:10",
        "Oracle APEX; filter by name/county/status; no required field",
    ),
    # ---- Tier 2: server-rendered but with friction ----
    BarTarget(
        "al",
        "Alabama",
        2,
        "https://members.alabar.org/Member_Portal/Member_Portal/Member-Search.aspx",
        "VERY slow; be polite",
        requires_param=True,
    ),
    BarTarget(
        "ak",
        "Alaska",
        2,
        "https://alaskabar.org/for-lawyers/member-directories/",
        "needs a param (country=United States); ~5185; Organization field",
        requires_param=True,
    ),
    BarTarget(
        "ct",
        "Connecticut",
        2,
        "https://www.jud.ct.gov/attorneyfirminquiry/attorneyfirminquiry.aspx",
        "ASP.NET; last name required; firm field present",
        requires_param=True,
    ),
    BarTarget(
        "de",
        "Delaware",
        2,
        "https://rp470541.doelegal.com/vwPublicSearch/Show-VwPublicSearch-Table.aspx",
        "Supreme Court doelegal; ~3.2k; firm field",
    ),
    BarTarget(
        "md",
        "Maryland",
        2,
        "https://www.mdcourts.gov/attysearch",
        "Judiciary AIS; partial last name; ~40k",
        requires_param=True,
    ),
    BarTarget(
        "me",
        "Maine",
        2,
        "https://apps.web.maine.gov/cgi-bin/online/maine_bar/attorney_directory.pl",
        "Board of Overseers; detail URLs predictable by bar_num",
    ),
    BarTarget(
        "mo",
        "Missouri",
        2,
        "https://mobar.org/public/LawyerDirectory.aspx",
        "mandatory; capped 250/query -> partial sweeps",
        requires_param=True,
    ),
    BarTarget(
        "pa",
        "Pennsylvania",
        2,
        "https://www.padisciplinaryboard.org/for-the-public/find-attorney",
        "Disciplinary Board; JS/AJAX results; ~75k",
        requires_param=True,
    ),
    BarTarget(
        "tx",
        "Texas",
        2,
        "https://www.texasbar.com/AM/Template.cfm?Section=Find_A_Lawyer&Template=%2FCustomSource%2FMemberDirectory%2FSearch_Form_Client_Main.cfm&Find=1",
        "~100k+; ColdFusion; by name/location/practice area",
        requires_param=True,
    ),
    BarTarget(
        "tn",
        "Tennessee",
        2,
        "https://www.tbpr.org/for-the-public/online-attorney-directory",
        "Board of Prof Resp; plain HTML form; firm NOT a distinct field",
        requires_param=True,
    ),
    BarTarget(
        "sc",
        "South Carolina",
        2,
        "https://www.sccourts.org/attorneys/",
        "SC Judicial; by first/last/bar#; firm not guaranteed",
        requires_param=True,
    ),
]


@dataclass
class Result:
    abbr: str
    name: str
    tier: int
    requested_url: str
    final_url: str = ""
    status: int | None = None
    verdict: str = ""
    visible_chars: int = 0
    n_forms: int = 0
    n_text_inputs: int = 0
    n_selects: int = 0
    n_tables: int = 0
    n_rows: int = 0
    n_scripts: int = 0
    title: str = ""
    generator: str = ""
    notes: list[str] = field(default_factory=list)
    error: str = ""


def _visible_text(tree: HTMLParser) -> str:
    for tag in tree.css("script, style, noscript, svg"):
        tag.decompose()
    body = tree.body or tree
    return " ".join((body.text() or "").split())


def characterize(req_url: str, resp: httpx.Response) -> Result:
    abbr = ""  # filled by caller
    r = Result(abbr=abbr, name="", tier=0, requested_url=req_url)
    r.final_url = str(resp.url)
    r.status = resp.status_code

    raw = resp.content
    body_low = raw[:20000].decode("utf-8", "ignore").lower()
    final_host = (urlparse(r.final_url).netloc or "").lower().removeprefix("www.")

    # Referral redirect?
    for h in REFERRAL_HOSTS:
        if final_host == h or final_host.endswith("." + h):
            r.verdict = "referral"
            r.notes.append(f"redirects to {final_host}")
            return r

    if resp.status_code >= 400:
        r.verdict = "error"
        r.notes.append(f"HTTP {resp.status_code}")
        # CF / WAF can return 403 with a challenge body.
        if any(m in body_low for m in CF_MARKERS):
            r.verdict = "cloudflare"
        return r

    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "html" not in ctype and "xml" not in ctype:
        r.verdict = "non_html"
        r.notes.append(f"content-type {ctype!r}")
        return r

    tree = HTMLParser(raw)
    title_node = tree.css_first("title")
    r.title = (title_node.text() if title_node else "").strip()[:120]
    gen = tree.css_first('meta[name="generator"]')
    if gen:
        r.generator = (gen.attributes.get("content") or "")[:60]

    r.n_forms = len(tree.css("form"))
    r.n_text_inputs = len(tree.css('input[type="text"], input[type="search"], input:not([type])'))
    r.n_selects = len(tree.css("select"))
    r.n_tables = len(tree.css("table"))
    r.n_rows = len(tree.css("tr"))
    r.n_scripts = len(tree.css("script"))
    text = _visible_text(tree)
    r.visible_chars = len(text)

    # Challenge gates (200-status soft blocks).
    if any(m in body_low for m in CF_MARKERS):
        r.verdict = "cloudflare"
        return r
    if any(m in body_low for m in CAPTCHA_MARKERS):
        r.verdict = "captcha"
        r.notes.append("captcha markers present")
        # not an immediate disqualifier if there's also a real form, but flag it
        if r.n_forms == 0 or r.visible_chars < 1500:
            return r

    # SPA shell detection: very little visible text + framework root.
    spa_root = bool(
        tree.css_first("#root, #app, [ng-app], [data-reactroot], [data-server-rendered]")
    )
    if r.visible_chars < 800 and (spa_root or r.n_scripts >= 5):
        r.verdict = "js_shell"
        r.notes.append(f"thin body ({r.visible_chars} chars), spa_root={spa_root}")
        return r

    # Has a real search form?
    if r.n_forms >= 1 and (r.n_text_inputs + r.n_selects) >= 1 and r.visible_chars >= 400:
        r.verdict = "server_form"
        return r

    if r.visible_chars < 800:
        r.verdict = "js_shell"
        r.notes.append(f"thin body ({r.visible_chars} chars), no clear form")
    else:
        r.verdict = "server_unclear"
        r.notes.append("rendered HTML but no obvious search form on landing page")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", type=str, default=None, help="comma list of abbrs to probe")
    ap.add_argument("--tier", type=int, default=None, help="restrict to a tier (1 or 2)")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between requests")
    args = ap.parse_args()

    targets = TARGETS
    if args.only:
        wanted = {s.strip().lower() for s in args.only.split(",")}
        targets = [t for t in targets if t.abbr in wanted]
    if args.tier:
        targets = [t for t in targets if t.tier == args.tier]

    out_dir = ROOT / "data" / "raw" / "state_bars" / date.today().isoformat() / "recon"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"== State-bar recon: {len(targets)} targets ==\n")
    results: list[Result] = []
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        for i, t in enumerate(targets):
            try:
                resp = client.get(t.url)
                r = characterize(t.url, resp)
                # persist raw html
                (out_dir / f"{t.abbr}.html.gz").write_bytes(gzip.compress(resp.content))
            except Exception as exc:
                r = Result(abbr=t.abbr, name=t.name, tier=t.tier, requested_url=t.url)
                r.verdict = "error"
                r.error = f"{type(exc).__name__}: {exc}"
            r.abbr, r.name, r.tier = t.abbr, t.name, t.tier
            if t.requires_param and r.verdict == "server_unclear":
                r.notes.append("landing page is a form (needs a search param to show rows)")
                r.verdict = "server_form"
            results.append(r)
            (out_dir / f"{t.abbr}.json").write_text(
                json.dumps(r.__dict__, indent=2), encoding="utf-8"
            )
            print(
                f"  [{t.tier}] {t.abbr:3s} {r.verdict:14s} "
                f"status={r.status} text={r.visible_chars:>6} "
                f"forms={r.n_forms} inputs={r.n_text_inputs} sel={r.n_selects} "
                f"tbl={r.n_tables} rows={r.n_rows} | {t.name}"
            )
            if r.notes:
                print(f"        notes: {'; '.join(r.notes)}")
            if r.error:
                print(f"        error: {r.error}")
            if i < len(targets) - 1:
                time.sleep(args.delay)

    (out_dir / "_summary.json").write_text(
        json.dumps([r.__dict__ for r in results], indent=2), encoding="utf-8"
    )

    # Verdict roll-up
    print("\n== Roll-up by verdict ==")
    by_v: dict[str, list[str]] = {}
    for r in results:
        by_v.setdefault(r.verdict, []).append(r.abbr)
    for v in sorted(by_v):
        print(f"  {v:16s} ({len(by_v[v]):2d}): {', '.join(by_v[v])}")
    print(f"\nRaw + characterization saved under {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
