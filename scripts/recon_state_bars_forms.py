"""Dump <form> structure from the saved state-bar recon HTML (no network).

For each saved data/raw/state_bars/{date}/recon/{abbr}.html.gz, print every
form's method, action, and the name=/value of its inputs/selects/buttons, plus
any reCAPTCHA presence and likely AJAX/JSON endpoints referenced in inline JS.
This tells us the actual search mechanism per state so we can write configs.

Usage:
    .venv/Scripts/python.exe scripts/recon_state_bars_forms.py --only wy,or,fl,ak,nd,ky,mt
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from selectolax.parser import HTMLParser  # noqa: E402

# crude endpoint sniffers for inline JS / fetch / form posts
URL_RE = re.compile(
    r"""['"](/[A-Za-z0-9_\-./]+(?:\.(?:asp|aspx|cfm|php|json|do|pl|ashx|svc)|/api/[^'"]*))['"]"""
)
RECAPTCHA_RE = re.compile(r"recaptcha|g-recaptcha|grecaptcha|hcaptcha", re.I)

# Association-management / CMS platform fingerprints. The hypothesis is that
# state bars run on a handful of shared platforms, so the right abstraction is
# ONE extractor per platform, not per state. A page can match several; we
# report all. Markers are matched case-insensitively in the page HTML.
PLATFORM_FINGERPRINTS: dict[str, tuple[str, ...]] = {
    "HigherLogic-Community": (
        "higherlogic",
        "connectedcommunity",
        "informz",
        "/network/members",
        "communities/community-home",
        "imis-",
    ),
    "iMIS-eWeb": ("/eweb/", "dynamicpage.aspx", "asicommon", "asi_", "imisform"),
    "Personify": ("personifyebusiness", "personify", "usr_id="),
    "Memberize-cv5": ("cv5/cgi-bin", "utilities.dll", "memberdll.dll", "vieth"),
    "ReliaGuide": ("reliaguide",),
    "Salesforce": ("force.com", "/s/sfsites", "lightning/", "aura_"),
    "Pega/Incapsula": ("prweb", "pega", "_incapsula_", "incident id"),
    "OracleAPEX": ("/ords/", "apex_", "wwv_flow"),
    "Sitefinity": ("sitefinity", "telerik"),
    "SharePoint": ("_layouts/", "sharepoint", "msocontent"),
    "ColdFusion": (".cfm", "cfid=", "cftoken="),
    "WordPress": ("wp-content", "wp-json", "wp-includes"),
    "Drupal": ("/sites/default/files", "drupal-settings-json", "drupal.js"),
    "Wix": ("wix.com", "wixstatic"),
    "Squarespace": ("squarespace",),
    "ASP.NET-WebForms": ("__viewstate", "__eventvalidation"),
}


def detect_platforms(html: bytes) -> list[str]:
    low = html[:300000].decode("utf-8", "ignore").lower()
    hits = []
    for plat, markers in PLATFORM_FINGERPRINTS.items():
        if any(m in low for m in markers):
            hits.append(plat)
    return hits


def dump(abbr: str, html: bytes) -> None:
    tree = HTMLParser(html)
    print(f"\n===== {abbr.upper()} =====")
    has_rc = bool(RECAPTCHA_RE.search(html[:60000].decode("utf-8", "ignore")))
    print(f"recaptcha/hcaptcha present in head/body: {has_rc}")

    forms = tree.css("form")
    print(f"forms: {len(forms)}")
    for fi, f in enumerate(forms):
        action = f.attributes.get("action") or "(self)"
        method = (f.attributes.get("method") or "GET").upper()
        fid = f.attributes.get("id") or f.attributes.get("name") or ""
        fields = []
        for inp in f.css("input, select, textarea, button"):
            nm = inp.attributes.get("name")
            if not nm:
                continue
            typ = inp.attributes.get("type") or inp.tag
            val = inp.attributes.get("value") or ""
            if nm.lower() in ("__viewstate", "__viewstategenerator", "__eventvalidation"):
                val = f"<{len(val)} chars>"
            opts = ""
            if inp.tag == "select":
                opt_vals = [
                    o.attributes.get("value") or (o.text() or "").strip() for o in inp.css("option")
                ][:6]
                opts = f" opts={opt_vals}"
            fields.append(f"      {typ:10s} {nm}={val[:40]!r}{opts}")
        rc = (
            "  [RECAPTCHA in form]"
            if f.css_first("[class*=recaptcha], [class*=g-recaptcha], [data-sitekey]")
            else ""
        )
        print(f"  form#{fi} id={fid!r} method={method} action={action!r}{rc}")
        for line in fields[:30]:
            print(line)
        if len(fields) > 30:
            print(f"      ... (+{len(fields) - 30} more fields)")

    # candidate endpoints referenced in JS / markup
    body_txt = html[:200000].decode("utf-8", "ignore")
    eps = sorted(set(m.group(1) for m in URL_RE.finditer(body_txt)))
    eps = [e for e in eps if not e.endswith((".css", ".js"))][:18]
    if eps:
        print("  candidate endpoints in markup/JS:")
        for e in eps:
            print(f"      {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", type=str, default=None)
    ap.add_argument("--date", type=str, default=date.today().isoformat())
    ap.add_argument(
        "--platforms",
        action="store_true",
        help="fingerprint the AMS/CMS platform per state + a summary, instead of dumping forms",
    )
    args = ap.parse_args()

    recon_dir = ROOT / "data" / "raw" / "state_bars" / args.date / "recon"
    files = sorted(recon_dir.glob("*.html.gz"))
    if args.only:
        wanted = {s.strip().lower() for s in args.only.split(",")}
        files = [f for f in files if f.name.split(".")[0] in wanted]

    if args.platforms:
        from collections import Counter

        summary: Counter[str] = Counter()
        print(f"== platform fingerprints ({len(files)} states) ==")
        for f in files:
            abbr = f.name.split(".")[0]
            plats = detect_platforms(gzip.decompress(f.read_bytes()))
            for p in plats:
                summary[p] += 1
            print(f"  {abbr:3s} {', '.join(plats) or '(none detected)'}")
        print("\n== platform summary (states per platform) ==")
        for plat, n in summary.most_common():
            print(f"  {n:2d}  {plat}")
        return 0

    for f in files:
        abbr = f.name.split(".")[0]
        dump(abbr, gzip.decompress(f.read_bytes()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
