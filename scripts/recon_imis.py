"""iMIS state-bar directory recon: confirm what the PUBLIC results expose.

AL and MO run iMIS (RiSE QueryMenu / Lawyer Directory web parts). iMIS search
is an ASP.NET postback: a GET seeds __VIEWSTATE + __RequestVerificationToken +
a session cookie, then a POST with those + the query field + the search
button's event target returns the results grid. This script replays that for a
last-name search and dumps the result columns so we can see whether firm /
address / phone come back publicly (vs members-only) BEFORE building an iMIS
adapter.

Reusable for other iMIS bars (DC, OK, ...): add their search-page URL.

Usage:
    uv run python scripts/recon_imis.py            # AL + MO, lastname=smith
    uv run python scripts/recon_imis.py --state al --lastname jones
"""

from __future__ import annotations

import argparse
import gzip
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
from selectolax.parser import HTMLParser  # noqa: E402

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PAGES = {
    "al": "https://members.alabar.org/Member_Portal/Member_Portal/Member-Search.aspx",
    "mo": "https://mobar.org/public/LawyerDirectory.aspx",
}

OUT = ROOT / "data" / "raw" / "state_bars" / date.today().isoformat() / "imis_probe"


def _label_for(inp) -> str:
    node = inp
    for _ in range(5):
        node = node.parent
        if node is None:
            break
        for cand in node.css("label, span, td, th"):
            txt = " ".join((cand.text() or "").split())
            if txt and 1 < len(txt) < 30 and not txt.startswith("ctl"):
                return txt
    return ""


def probe(state: str, lastname: str) -> None:
    page_url = PAGES[state]
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"\n===== {state.upper()} iMIS probe (lastname={lastname!r}) =====")
    with httpx.Client(headers=HEADERS, timeout=40.0, follow_redirects=True) as c:
        r0 = c.get(page_url)
        t0 = HTMLParser(r0.content)
        form = t0.css_first("form#aspnetForm") or t0.css_first("form")
        if form is None:
            print("  no form found")
            return
        action = urljoin(str(r0.url), form.attributes.get("action") or str(r0.url))

        data: dict[str, str] = {}
        lastname_field = None
        for inp in form.css("input, select, textarea"):
            nm = inp.attributes.get("name")
            if not nm:
                continue
            data[nm] = inp.attributes.get("value") or ""
            typ = (inp.attributes.get("type") or inp.tag).lower()
            if typ in ("text", "search", "textarea", "input"):
                lbl = _label_for(inp).lower()
                if "last name" in lbl or lbl == "last name contains:":
                    lastname_field = nm

        if lastname_field:
            data[lastname_field] = lastname
            print(f"  last-name field: ...{lastname_field[-40:]}")
        else:
            print("  ! could not locate last-name field; results may be empty")

        # Find the search trigger: a submit input/button, or a __doPostBack target
        # near a "Search"/"Find" control.
        triggers = []
        for el in form.css(
            "input[type=submit], button, a[href*=__doPostBack], a[onclick*=__doPostBack]"
        ):
            txt = (el.attributes.get("value") or el.text() or "").strip().lower()
            nm = el.attributes.get("name") or ""
            onclick = (el.attributes.get("href") or "") + (el.attributes.get("onclick") or "")
            if any(k in txt for k in ("search", "find", "go", "submit")):
                triggers.append((txt, nm, onclick[:80]))
        print(f"  search-trigger candidates: {triggers[:4]}")
        # iMIS submit buttons fire __doPostBack(controlName) -> set the event
        # target to the trigger's control name (works for both submit inputs
        # and __doPostBack anchors).
        import re

        for _txt, nm, onclick in triggers:
            if nm:
                data["__EVENTTARGET"] = nm
                data["__EVENTARGUMENT"] = ""
                if (inp_el := form.css_first(f'input[name="{nm}"]')) is not None:
                    data[nm] = inp_el.attributes.get("value") or "Find"
                break
            m = re.search(r"__doPostBack\('([^']+)'", onclick)
            if m:
                data["__EVENTTARGET"] = m.group(1)
                data["__EVENTARGUMENT"] = ""
                break

        r1 = c.post(action, data=data)
        (OUT / f"{state}_result.html.gz").write_bytes(gzip.compress(r1.content))
        t1 = HTMLParser(r1.content)
        print(f"  POST -> {r1.status_code}  bytes={len(r1.content)}")

        # Results grid: find the largest table; dump header + first 2 data rows.
        tables = [tb for tb in t1.css("table") if len(tb.css("tr")) >= 2]
        tables.sort(key=lambda tb: len(tb.css("tr")), reverse=True)
        if not tables:
            print("  no results table found (auth-gated? or no rows)")
        for tb in tables[:1]:
            rows = tb.css("tr")
            print(f"  results table: {len(rows)} rows")
            for ri, row in enumerate(rows[:4]):
                cells = [" ".join((cl.text() or "").split())[:24] for cl in row.css("th,td")]
                print(f"    row{ri}: {cells}")
        low = r1.content.decode("utf-8", "ignore").lower()
        print(
            "  result has:",
            {k: (k in low) for k in ("firm", "company", "suite", "phone", "@", "esq")},
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", choices=sorted(PAGES), default=None)
    ap.add_argument("--lastname", default="smith")
    args = ap.parse_args()
    states = [args.state] if args.state else list(PAGES)
    for st in states:
        try:
            probe(st, args.lastname)
        except Exception as exc:  # recon tool: report and keep going
            print(f"  {st} ERROR: {type(exc).__name__}: {exc}")
    print(f"\nsaved result HTML under {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
