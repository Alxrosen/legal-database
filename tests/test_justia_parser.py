"""Tests for the Justia parser + office aggregation.

Justia is attorney-level with no firm name; firm-shaped records are
built by grouping lawyers on normalized office (street, city). These
tests pin the card field extraction, the br-split address parse, the
"Next"-link pagination signal, and the office grouping (incl. the
solo / no-address fallback).
"""

from __future__ import annotations

from legal_sourcing.parsers.justia import JustiaDirectoryParser, extract_page_meta
from legal_sourcing.pipelines.scrape_az_bar import normalize_record
from legal_sourcing.pipelines.scrape_justia import aggregate_by_office

# Two organic cards sharing one office + one premium card with no address.
_HTML = """
<html><body>
<div class="jld-card -organic" data-vars-profile="111">
  <strong class="name"><a href="https://lawyers.justia.com/lawyer/jane-smith-111">Jane Smith</a></strong>
  <a href="tel:6021111111" class="phone">(602) 111-1111</a>
  <div class="address">100 Main St<br>Suite 5<br>Phoenix,\t\tAZ 85001</div>
  <div class="outline">Personal Injury and Divorce</div>
  <a class="website" href="https://janesmithlaw.com">View Website</a>
</div>
<div class="jld-card -organic" data-vars-profile="222">
  <strong class="name"><a href="https://lawyers.justia.com/lawyer/bob-jones-222">Bob Jones</a></strong>
  <a href="tel:6022222222" class="phone">(602) 222-2222</a>
  <div class="address">100 Main St<br>Suite 5<br>Phoenix, AZ 85001</div>
  <div class="outline">Personal Injury</div>
  <a class="website" href="https://justia.lawyer/bob-jones-222">View Website</a>
</div>
<div class="jld-card -premium -gold" data-vars-profile="333">
  <strong class="name"><a href="https://lawyers.justia.com/lawyer/sally-roe-333">Sally Roe</a></strong>
  <a href="tel:6023333333" class="phone">(602) 333-3333</a>
  <div class="outline">Lawyer Serving Arizona</div>
</div>
<div class="pagination"><a href="/lawyers/arizona?page=2">Next</a></div>
</body></html>
"""


def _parsed():
    return JustiaDirectoryParser().parse_bytes(_HTML.encode(), source_url="x")


def test_parse_cards_basic_fields():
    recs = _parsed()
    assert len(recs) == 3
    jane = recs[0]
    assert jane["name_raw"] is None  # no firm name on Justia
    c = jane["contacts"][0]
    assert c["name_raw"] == "Jane Smith"
    assert c["source_attorney_id"] == "111"
    assert jane["phone_raw"] == "(602) 111-1111"
    assert jane["website_raw"] == "https://janesmithlaw.com"
    o = jane["offices"][0]
    assert o["street_raw"] == "100 Main St Suite 5"
    assert o["city_raw"] == "Phoenix"
    assert o["state_raw"] == "AZ"
    assert o["postal_code_raw"] == "85001"
    assert jane["practice_areas_raw"] == ["Personal Injury", "Divorce"]


def test_website_excludes_justia_microsites():
    # Bob's only "website" is a justia.lawyer microsite -> must be dropped
    # (it normalizes to a shared domain and would cause false matches).
    bob = _parsed()[1]
    assert bob["contacts"][0]["name_raw"] == "Bob Jones"
    assert bob["website_raw"] is None


def test_premium_card_has_no_address_or_practice():
    sally = _parsed()[2]
    assert sally["offices"] == []
    assert sally["practice_areas_raw"] == []  # "Lawyer Serving ..." is filtered
    assert sally["contacts"][0]["name_raw"] == "Sally Roe"


def test_extract_page_meta_next_link():
    meta = extract_page_meta(_HTML)
    assert meta["card_count"] == 3
    assert meta["has_next"] is True


def test_aggregate_by_office_groups_shared_office_and_solo():
    recs = _parsed()
    for r in recs:
        normalize_record(r)
    firms = aggregate_by_office(recs)
    # Jane + Bob share one office -> 1 firm; Sally (no address) -> solo.
    assert len(firms) == 2
    by_count = sorted(firms, key=lambda f: -f["attorney_count"])
    office_firm, solo_firm = by_count[0], by_count[1]
    assert office_firm["attorney_count"] == 2
    names = {c["name_raw"] for c in office_firm["contacts"]}
    assert names == {"Jane Smith", "Bob Jones"}
    # practice areas unioned across the two lawyers
    assert "Personal Injury" in office_firm["practice_areas_raw"]
    assert "Divorce" in office_firm["practice_areas_raw"]
    assert solo_firm["attorney_count"] == 1
    assert solo_firm["contacts"][0]["name_raw"] == "Sally Roe"
    # stable, distinct source_firm_ids
    ids = {f["source_firm_id"] for f in firms}
    assert len(ids) == 2
