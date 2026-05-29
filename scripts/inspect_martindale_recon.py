"""Quick inspection of the Martindale recon fixtures.

Throwaway diagnostic — not part of the production pipeline.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
F = ROOT / "tests" / "fixtures" / "martindale" / "recon"


def main() -> None:
    states = json.loads((F / "state_index.json").read_text(encoding="utf-8"))
    print(f"== state_index ==")
    print(f"  n = {len(states)}")
    print(f"  first 6: {[s['name'] for s in states[:6]]}")
    print(f"  last 6:  {[s['name'] for s in states[-6:]]}")
    prefixes = Counter()
    for s in states:
        p = urlparse(s["url"]).path.strip("/")
        prefixes[p.split("/")[0] if p else "?"] += 1
    print(f"  URL path prefixes: {dict(prefixes)}")
    real_states = {
        s["name"]
        for s in states
        if urlparse(s["url"]).path.startswith("/by-location/")
    }
    print(f"  links under /by-location/: {len(real_states)} (expect ~50)")
    suspicious = [s for s in states if not urlparse(s["url"]).path.startswith("/by-location/")]
    if suspicious:
        print(f"  NOT under /by-location/: {len(suspicious)}")
        for s in suspicious[:5]:
            print(f"    {s}")

    print()
    print("== abbeville_alabama_attorneys ==")
    cards = json.loads((F / "abbeville_alabama_attorneys.json").read_text(encoding="utf-8"))
    print(f"  cards n = {len(cards)}")
    print(f"  with attorney_name: {sum(1 for c in cards if c.get('attorney_name'))}")
    print(f"  with firm_name_raw: {sum(1 for c in cards if c.get('firm_name_raw'))}")
    print(f"  with title_raw   : {sum(1 for c in cards if c.get('title_raw'))}")
    print(f"  with phone       : {sum(1 for c in cards if c.get('phone'))}")
    print(f"  with location    : {sum(1 for c in cards if c.get('location_text'))}")

    no_attorney = [c for c in cards if not c.get("attorney_name")]
    print(f"  cards lacking attorney_name: {len(no_attorney)}")
    if no_attorney:
        print(f"    sample: {no_attorney[0]}")

    print()
    print("  sample card WITH firm:")
    for c in cards:
        if c.get("firm_name_raw"):
            print(f"    {json.dumps(c, indent=4)}")
            break

    print()
    print("== firm profile ==")
    profile = json.loads((F / "sample_firm_profile.json").read_text(encoding="utf-8"))
    print(f"  {profile}")


if __name__ == "__main__":
    main()
