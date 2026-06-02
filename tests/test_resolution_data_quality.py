"""Guards that keep the resolution inputs clean.

Covers the data-quality fixes found while auditing the DB before
canonical resolution:
  * aggregator/directory/social domains never become a website key;
  * Martindale offices get their state backfilled from the swept URL
    (cards show only the city), so primary_state / name_state blocking
    work;
  * the state slug -> USPS abbreviation map is complete.
"""

from __future__ import annotations

from legal_sourcing.geo import STATE_SLUG_TO_ABBR, US_STATE_SLUGS
from legal_sourcing.normalize.url import is_aggregator_domain
from legal_sourcing.pipelines.scrape_az_bar import normalize_record
from legal_sourcing.pipelines.scrape_martindale import _backfill_office_state

# ---- aggregator-domain denylist -------------------------------------------


def test_is_aggregator_domain():
    assert is_aggregator_domain("lawfirms.com")
    assert is_aggregator_domain("westcoast.lawfirms.com")  # subdomain
    assert is_aggregator_domain("avvo.com")
    assert is_aggregator_domain("facebook.com")
    assert is_aggregator_domain("linkedin.com")
    assert not is_aggregator_domain("smithlaw.com")
    assert not is_aggregator_domain("mylawfirms.com")  # look-alike, not a subdomain
    assert not is_aggregator_domain(None)


def test_normalize_record_drops_aggregator_website():
    agg = normalize_record(
        {"name_raw": "Acme Law", "website_raw": "https://westcoast.lawfirms.com/x"}
    )
    assert agg["website_normalized"] is None
    real = normalize_record({"name_raw": "Acme Law", "website_raw": "https://acmelaw.com"})
    assert real["website_normalized"] == "acmelaw.com"


# ---- state slug -> abbreviation map ---------------------------------------


def test_state_slug_to_abbr_complete():
    # every national slug has an abbreviation, all unique, all 2 chars
    assert set(STATE_SLUG_TO_ABBR) == set(US_STATE_SLUGS)
    abbrs = list(STATE_SLUG_TO_ABBR.values())
    assert len(set(abbrs)) == len(abbrs)
    assert all(len(a) == 2 and a.isupper() for a in abbrs)
    assert STATE_SLUG_TO_ABBR["arizona"] == "AZ"
    assert STATE_SLUG_TO_ABBR["district-of-columbia"] == "DC"


# ---- Martindale state backfill --------------------------------------------


def test_backfill_office_state_fills_missing_then_normalizes():
    rec = {
        "name_raw": "Doe Law",
        "offices": [{"city_raw": "Phoenix", "state_raw": None, "country_raw": "US"}],
        "contacts": [],
        "practice_areas_raw": [],
    }
    _backfill_office_state([rec], "arizona")
    assert rec["offices"][0]["state_raw"] == "AZ"
    normalize_record(rec)
    assert rec["offices"][0]["normalized"]["state"] == "AZ"


def test_backfill_office_state_does_not_override_existing():
    rec = {"offices": [{"city_raw": "Yuma", "state_raw": "CA"}], "contacts": []}
    _backfill_office_state([rec], "arizona")  # card already had a (wrong-but-present) state
    assert rec["offices"][0]["state_raw"] == "CA"
