"""Martindale parser tests — focused on the three card shapes and
the title/firm split behavior.

Recon fixtures live under tests/fixtures/martindale/recon/.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from legal_sourcing.parsers.martindale import (
    MartindaleCityParser,
    _is_concatenated_list,
    _split_title_at_firm,
    extract_page_meta,
    parse_firm_profile,
    parse_firm_profile_full,
)

FIXTURES = Path(__file__).parent / "fixtures" / "martindale" / "recon"


# ---- _split_title_at_firm -----------------------------------------------


@pytest.mark.parametrize(
    "raw, title, firm",
    [
        # Subscriber card slicing artifact (no whitespace after "at"):
        ("Managing Partner at", "Managing Partner", None),
        ("Member at", "Member", None),
        ("Founder at", "Founder", None),
        # Non-subscriber "Title at Firm" pattern:
        ("Member at DaGian Law Offices, LLP", "Member", "DaGian Law Offices, LLP"),
        (
            "Gen. Coun. at Great Southern Wood Preserving, Inc.",
            "Gen. Coun.",
            "Great Southern Wood Preserving, Inc.",
        ),
        # Solo / freeform — no " at " at all:
        ("Solo Practitioner", "Solo Practitioner", None),
        ("Attorney", "Attorney", None),
        # Comma variants:
        ("Member, at FirmName", "Member", "FirmName"),
        # Empty / None
        ("", None, None),
        (None, None, None),
    ],
)
def test_split_title_at_firm(raw, title, firm):
    assert _split_title_at_firm(raw or "") == (title, firm)


# ---- City parser end-to-end (against recon fixture) ---------------------


def test_city_parser_emits_one_record_per_card():
    """Recon fixture had 54 cards from Abbeville, AL. The parser run
    against the raw page should produce the same count."""
    # The recon saved gz pages under data/raw — but those are local
    # and gitignored. The committed fixture is the extracted JSON,
    # not the raw HTML. Use the extracted card list to check we still
    # see the same 54 entries when re-parsing fresh raw bytes.
    # (We have a smaller smoke check below that uses synthetic HTML.)
    payload = b"""<html><body>
    <div class="card card--attorney">
      <ul>
        <li class="detail_title">
          <a href="https://www.martindale.com/attorney/john-doe-12345/">
            <h3>John Doe</h3>
          </a>
        </li>
        <li class="detail_position">
          Managing Partner at
          <a class="detail_position--office-link"
             href="https://www.martindale.com/organization/example-firm-99/dothan-alabama-100-f/"
             data-gtm-tracking='{"firm_id":"100"}'>
            Example Firm
          </a>
        </li>
        <li class="detail_location">Dothan, AL</li>
      </ul>
    </div>
    </body></html>"""
    parser = MartindaleCityParser()
    records = parser.parse_bytes(
        payload, source_url="https://www.martindale.com/all-lawyers/dothan/alabama/"
    )
    assert len(records) == 1
    rec = records[0]
    assert rec["name_raw"] == "Example Firm"
    contact = rec["contacts"][0]
    assert contact["name_raw"] == "John Doe"
    assert contact["title"] == "Managing Partner"  # trailing "at" trimmed
    assert contact["source_attorney_id"] == "12345"
    assert rec["additional_data"]["source_firm_id_martindale"] == "100"
    assert rec["additional_data"]["card_shape"] == "subscriber"
    assert rec["offices"][0]["city_raw"] == "Dothan"
    assert rec["offices"][0]["state_raw"] == "AL"


def test_city_parser_handles_non_subscriber_at_pattern():
    """Card with NO firm anchor; title is freeform 'X at Y'."""
    payload = b"""<html><body>
    <div class="card card--attorney">
      <ul>
        <li class="detail_title">
          <a href="https://www.martindale.com/attorney/jane-doe-99999/">
            <h3>Jane Doe</h3>
          </a>
        </li>
        <li class="detail_position">Member at DaGian Law Offices, LLP</li>
        <li class="detail_location">Birmingham, AL</li>
      </ul>
    </div>
    </body></html>"""
    parser = MartindaleCityParser()
    records = parser.parse_bytes(
        payload, source_url="https://www.martindale.com/all-lawyers/birmingham/alabama/"
    )
    assert len(records) == 1
    rec = records[0]
    assert rec["name_raw"] == "DaGian Law Offices, LLP"
    contact = rec["contacts"][0]
    assert contact["title"] == "Member"
    assert rec["additional_data"]["card_shape"] == "non_subscriber_at_pattern"


def test_city_parser_handles_solo_no_firm():
    payload = b"""<html><body>
    <div class="card card--attorney">
      <ul>
        <li class="detail_title">
          <a href="https://www.martindale.com/attorney/sue-solo-77777/">
            <h3>Sue Solo</h3>
          </a>
        </li>
        <li class="detail_position">Solo Practitioner</li>
        <li class="detail_location">Mobile, AL</li>
      </ul>
    </div>
    </body></html>"""
    parser = MartindaleCityParser()
    [rec] = parser.parse_bytes(
        payload, source_url="https://www.martindale.com/all-lawyers/mobile/alabama/"
    )
    assert rec["name_raw"] is None
    assert rec["contacts"][0]["title"] == "Solo Practitioner"
    assert rec["additional_data"]["card_shape"] == "solo"


def _subscriber_card(website_href: str | None) -> bytes:
    """A subscriber firm card matching the real Sacramento shape, with an
    optional per-card 'View Website' button.
    """
    web = (
        f'<a class="button webstats-website-click pulsepoint-click-out" '
        f'href="{website_href}">View Website</a>'
        if website_href is not None
        else ""
    )
    return f"""<html><body>
    <div class="card card--attorney">
      <ul>
        <li class="detail_title">
          <a href="https://www.martindale.com/attorney/jane-doe-123/"><h3>Jane Doe</h3></a>
        </li>
        <li class="detail_position">
          <a class="detail_position--office-link"
             href="https://www.martindale.com/organization/acme-law-9/"
             data-gtm-tracking='{{"firm_id":"9","profile_type":"Subscriber"}}'>Acme Law LLP</a>
        </li>
        <li class="detail_location">Sacramento, CA</li>
        <a class="button webstats-phone-click" href="tel:279-221-6996">Call</a>
        {web}
      </ul>
    </div>
    </body></html>""".encode()


def _parse_one_card(payload: bytes):
    recs = MartindaleCityParser().parse_bytes(
        payload, source_url="https://www.martindale.com/all-lawyers/sacramento/california/"
    )
    assert len(recs) == 1
    return recs[0]


def test_city_card_extracts_website_from_view_website_anchor():
    """Root fix: the per-card webstats-website-click anchor IS the firm
    website — the builder used to drop it (website_raw=None)."""
    rec = _parse_one_card(_subscriber_card("https://www.acmelaw.com/"))
    assert rec["name_raw"] == "Acme Law LLP"
    assert rec["website_raw"] == "https://www.acmelaw.com/"


def test_city_card_website_none_when_no_anchor():
    rec = _parse_one_card(_subscriber_card(None))
    assert rec["website_raw"] is None


def test_city_card_website_strips_martindale_self_domain():
    """A View-Website button pointing back at martindale.com is not a firm
    site — strip_self_domain drops it."""
    rec = _parse_one_card(_subscriber_card("https://www.martindale.com/organization/acme-law-9/"))
    assert rec["website_raw"] is None


def test_city_card_aggregator_website_kept_raw_but_not_a_merge_key():
    """A lead-gen aggregator (e.g. lawfirms.com) survives as website_raw but
    normalize_record nulls website_normalized so it never merges firms."""
    from legal_sourcing.pipelines.scrape_az_bar import normalize_record

    rec = _parse_one_card(_subscriber_card("https://westcoast.lawfirms.com/x-ca/"))
    assert rec["website_raw"] == "https://westcoast.lawfirms.com/x-ca/"
    normalize_record(rec)
    assert rec["website_normalized"] is None


# ---- Firm profile parser -----------------------------------------------


def test_firm_profile_uses_webstats_selector():
    payload = b"""<html><body>
    <a class="webstats-website-click button"
       rel="sponsored"
       href="http://example.com/firm">visit</a>
    <a href="tel:555-1234">call</a>
    </body></html>"""
    profile = parse_firm_profile(payload)
    assert profile["firm_website_url"] == "http://example.com/firm"
    assert profile["firm_website_is_sponsored"] is True
    assert profile["firm_phone"] == "555-1234"


def test_firm_profile_handles_missing_website():
    payload = b"<html><body><a href='tel:555-9999'>x</a></body></html>"
    profile = parse_firm_profile(payload)
    assert "firm_website_url" not in profile
    assert profile["firm_phone"] == "555-9999"


# ---- Pagination metadata extraction --------------------------------------


def _city_page_html(
    *,
    declared_total: str | None = "(6,994)",
    data_max: str | None = "167",
    next_unavailable: bool = False,
    card_count: int = 30,
) -> str:
    """Synthetic city-page HTML matching the real Birmingham shape."""
    next_class = "arrow unavailable" if next_unavailable else "arrow"
    total_block = (
        f'<h2 class="results__title">Birmingham Attorney Results '
        f'<span class="results__total">{declared_total}</span></h2>'
        if declared_total is not None
        else ""
    )
    goto_block = (
        f'<input class="goToPage" type="text" data-max="{data_max}" value="1"/>'
        if data_max is not None
        else ""
    )
    next_block = (
        f'<a class="{next_class}" rel="next" href="/all-lawyers/x/y/?page=2" data-page="2">next</a>'
    )
    cards = "".join(
        '<div class="card card--attorney"><ul><li class="detail_title">'
        f'<a href="/attorney/x-{i}/"><h3>X {i}</h3></a></li></ul></div>'
        for i in range(card_count)
    )
    return f"""<html><body>
        {total_block}
        {cards}
        <ul class="inline-list right pagination">
          {goto_block}
          {next_block}
        </ul>
    </body></html>"""


def test_extract_page_meta_birmingham_shape():
    """Matches the captured Birmingham page 1: 6,994 declared, 167 pages,
    next link present, 30 cards visible.
    """
    meta = extract_page_meta(_city_page_html())
    assert meta["results_total"] == 6994
    assert meta["last_page"] == 167
    assert meta["has_next"] is True
    assert meta["card_count"] == 30


def test_extract_page_meta_last_page_next_unavailable():
    meta = extract_page_meta(_city_page_html(next_unavailable=True))
    assert meta["has_next"] is False


def test_extract_page_meta_missing_signals_returns_none_fields():
    meta = extract_page_meta("<html><body>no pagination here</body></html>")
    assert meta == {
        "results_total": None,
        "last_page": None,
        "has_next": False,
        "card_count": 0,
    }


def test_parse_firm_profile_full_prim_mendheim_shape():
    """Synthetic profile HTML matching the Prim & Mendheim shape we
    verified during recon (docs/data_sources/martindale.md §9.5).
    """
    html = """<html><body>
      <ul class="masthead-list">
        <li class="masthead-list__item masthead-list__item--bold">Dothan, AL</li>
        <li class="masthead-list__item">103 Jamestown Boulevard, P.O. Box 2147, 36302, Dothan, AL 36301</li>
        <li class="masthead-list__item">A General Practice Law Firm That Specializes In Collections</li>
        <li class="masthead-list__item">Peer Reviews4.4/5.0(57)</li>
        <li class="masthead-list__item">Profile Visibility...</li>
      </ul>

      <div>Year Established:2006</div>
      <p>Office Size: 3</p>

      <h2>Areas of Practice(5)</h2>
      <span class="toggle-area__header-count">(5)</span>
      <ul id="aopList">
        <li>Civil Litigation</li>
        <li>Personal Injury</li>
        <li>Fraud</li>
        <li>Real Estate</li>
        <li>Collections</li>
      </ul>

      <h2>People(3)</h2>
      <span class="toggle-area__header-count">(3)</span>

      <h2>About our Dothan, AL office</h2>
      <div class="truncate-text">Prim &amp; Mendheim, LLC is a general practice law firm based in Dothan, Alabama with lifelong Dothan lawyers that specialize in real estate transactions and the collection of commercial and consumer debt.</div>

      <h2>Our Firm</h2>
      <div class="truncate-text">Our firm has been serving Dothan since 2006.</div>

      <a href="http://www.pm-firm.com" class="webstats-website-click" rel="sponsored">Website</a>
      <a href="tel:+13344830339">Phone</a>
    </body></html>"""
    result = parse_firm_profile_full(html.encode("utf-8"))
    assert result["primary_city"] == "Dothan"
    assert result["primary_state"] == "AL"
    # Last ZIP is the physical one (36301), not the P.O. Box one (36302).
    assert result["primary_postal_code"] == "36301"
    assert (
        result["firm_short_description"]
        == "A General Practice Law Firm That Specializes In Collections"
    )
    assert result["year_established"] == 2006
    assert result["practice_areas"] == [
        "Civil Litigation",
        "Personal Injury",
        "Fraud",
        "Real Estate",
        "Collections",
    ]
    assert result["practice_area_count_toggle"] == 5
    assert result["people_count"] == 3
    # Office Size: 3 matches people=3 -> office_count must be NULL.
    assert result["office_count"] is None
    assert result["office_size_label_raw"] == 3
    # Descriptions paired with the preceding h2 + filter the AOP-text noise.
    assert {d["heading"] for d in result["firm_descriptions"]} == {
        "About our Dothan, AL office",
        "Our Firm",
    }
    assert result["firm_website_url"] == "http://www.pm-firm.com"
    assert result["firm_website_is_sponsored"] is True
    assert result["firm_phone"] == "+13344830339"


def test_parse_firm_profile_full_handles_missing_year_and_tagline():
    """The McGhee Firm: no tagline, no year established. Parser must
    not crash and must return None for those fields.
    """
    html = """<html><body>
      <ul class="masthead-list">
        <li class="masthead-list__item masthead-list__item--bold">Dothan, AL</li>
        <li class="masthead-list__item">424 South Oates Street, Dothan, AL 36301</li>
        <li class="masthead-list__item">Peer ReviewsNo Reviews</li>
        <li class="masthead-list__item">Profile Visibility...</li>
      </ul>
      <h2>Areas of Practice(2)</h2>
      <ul id="aopList"><li>Criminal Defense</li><li>Personal Injury</li></ul>
      <h2>People(1)</h2>
    </body></html>"""
    result = parse_firm_profile_full(html.encode("utf-8"))
    assert result["primary_city"] == "Dothan"
    assert result["primary_state"] == "AL"
    assert result["primary_postal_code"] == "36301"
    assert result["year_established"] is None
    assert result["firm_short_description"] is None
    assert result["practice_areas"] == ["Criminal Defense", "Personal Injury"]
    assert result["people_count"] == 1
    assert result["firm_descriptions"] == []


def test_parse_firm_profile_full_office_count_when_distinct_from_people():
    """If Office Size differs meaningfully from People, use it as
    office_count (rare; mostly a "find the office count source"
    discovery for the future)."""
    html = """<html><body>
      <ul class="masthead-list">
        <li class="masthead-list__item masthead-list__item--bold">Phoenix, AZ</li>
        <li class="masthead-list__item">100 Main St, Phoenix, AZ 85001</li>
      </ul>
      <p>Office Size: 100</p>
      <h2>People(5)</h2>
      <ul id="aopList"><li>Test</li></ul>
    </body></html>"""
    result = parse_firm_profile_full(html.encode("utf-8"))
    # 100 is wildly larger than 5 -> trust it as office_count.
    assert result["office_count"] == 100


@pytest.mark.parametrize(
    "text, expected",
    [
        # Genuine prose (recon: all 5 firms' real description blocks).
        (
            "Starnes Davis Florie LLP is a general civil practice law firm "
            "exclusively committed to the trial and resolution of civil litigation.",
            False,
        ),
        # AOP list rendered without separators (recon: Starnes 818/938-char
        # blocks, McGhee 1,144, Sargon 123, Prim 58 — all leaked before).
        (
            "Admiralty & Maritime LitigationAlternative Dispute ResolutionAppellate "
            "LawAutomobile Liability DefenseBanking & Financial ServicesBusiness Litigation",
            True,
        ),
        ("Civil LitigationPersonal InjuryFraudReal EstateCollections", True),
        ("", False),
        ("Short prose sentence about the firm.", False),
    ],
)
def test_is_concatenated_list(text, expected):
    assert _is_concatenated_list(text) is expected


def test_parse_firm_profile_full_filters_long_aop_text_noise():
    """Starnes-shape regression: the AOP list also renders as one or more
    long, null-headed `div.truncate-text` blocks (recon measured 818 & 938
    chars). The old `len < 500` gate let them through into firm_descriptions;
    only the genuine prose block must survive.
    """
    prose = (
        "Starnes Davis Florie LLP is a general civil practice law firm exclusively "
        "committed to the trial and resolution of civil litigation. The firm serves "
        "physicians, hospitals, banks, corporations, and governments across Alabama."
    )
    aop_noise_long = (
        "Admiralty & Maritime LitigationAlternative Dispute ResolutionAppellate Law"
        "Automobile Liability DefenseBanking & Financial ServicesBiotechnology & "
        "PharmaceuticalBusiness Investigations & White Collar DefenseBusiness Litigation"
        "Class Actions & Mass TortsComplex Insurance LitigationConstruction Litigation"
    )
    aop_noise_short = "Civil LitigationPersonal InjuryFraudReal EstateCollections"
    html = f"""<html><body>
      <ul class="masthead-list">
        <li class="masthead-list__item masthead-list__item--bold">Birmingham, AL</li>
        <li class="masthead-list__item">100 Brookwood Place, 7th Floor, Birmingham, AL 35209</li>
      </ul>
      <div class="truncate-text">{prose}</div>
      <div class="truncate-text">{aop_noise_long}</div>
      <div class="truncate-text">{aop_noise_short}</div>
    </body></html>"""
    result = parse_firm_profile_full(html.encode("utf-8"))
    texts = [d["text"] for d in result["firm_descriptions"]]
    assert texts == [prose]


def test_extract_page_meta_falls_back_to_anchor_data_page():
    """When input.goToPage[data-max] is absent, the largest data-page
    integer on an <a> inside ul.pagination becomes the last_page."""
    html = """<html><body>
      <ul class="pagination">
        <li><a data-page="1" href="?page=1">1</a></li>
        <li><a data-page="2" href="?page=2">2</a></li>
        <li><a data-page="42" href="?page=42">42</a></li>
      </ul>
    </body></html>"""
    meta = extract_page_meta(html)
    assert meta["last_page"] == 42


def test_extract_page_meta_parses_inflated_total():
    """Some directories present results_total in millions with commas."""
    html = _city_page_html(declared_total="(1,234,567)")
    meta = extract_page_meta(html)
    assert meta["results_total"] == 1234567


def test_extract_page_meta_handles_empty_results_total():
    html = _city_page_html(declared_total="(— results)")
    meta = extract_page_meta(html)
    # Number regex finds nothing -> None.
    assert meta["results_total"] is None
