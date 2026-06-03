"""Tests for the firm-website extraction cascade.

Synthetic HTML fixtures model the real documented cases
(docs/data_sources/firm_websites.md §3/§4/§12): Goetz big-number H2s,
TEPLG team page, BIPC prose, Caritas stated count, a solo practice, and the
swissbiologic non-law site. Each pins one cascade branch.
"""

from __future__ import annotations

from legal_sourcing.enrichment.website_extract import (
    detect_platform,
    extract_headcount,
    extract_offices,
    extract_phones,
    extract_site,
    extract_years,
    relevance_gate,
)

# --- fixtures -------------------------------------------------------------

Goetz_HOME = """
<html><head><title>Goetz Law Group | Truck Accident Lawyers</title></head>
<body>
  <h2>Billions</h2><p>We have recovered billions of dollars for clients.</p>
  <h2>600+ Staff</h2><p>With three fully-staffed locations, our team supports you.</p>
  <h2>25+ Years</h2><p>Service to our community spans over two decades.</p>
  <h2>40+ Lawyers</h2><p>We have a deep bench of industry-leading attorneys.</p>
  <footer>
    Dallas, TX 75201 | Fort Worth, TX 76102 | Atlanta, GA 30303 | Chicago, IL 60601
    <a href="tel:18007770000">1-800-777-0000</a>
  </footer>
</body></html>
"""

TEPLG_TEAM = """
<html><head><title>Our Team | The Estate Planning Law Group</title></head><body>
  <div><h2>Bill Deitch</h2><p>Attorney &amp; Counselor at Law</p></div>
  <div><h2>Kirsten Izatt</h2><p>Attorney &amp; Counselor at Law</p></div>
  <div><h2>Kathleen DiCola</h2><p>Attorney &amp; Counselor at Law, Of Counsel</p></div>
  <div><h2>Dawn Neumann</h2><p>Administrative Assistant</p></div>
  <div><h2>Kristen Oakley</h2><p>Paralegal</p></div>
  <div><h2>Stephanie Rath</h2><p>Trust &amp; Estate Coordinator</p></div>
  <div><h2>Dianna Weglarz</h2><p>Senior Paralegal</p></div>
</body></html>
"""

CARITAS_TEAM = """
<html><head><title>Our Attorneys</title></head><body>
  <h1>Our Team</h1><p>Caritas Law Group is proud of our 20 Attorneys serving Arizona.</p>
</body></html>
"""

BIPC_ABOUT = """
<html><head><title>About Buchanan Ingersoll &amp; Rooney</title></head><body>
<p>Buchanan Ingersoll &amp; Rooney is a national law firm. Our 450 attorneys and
government relations professionals across 16 offices proudly represent some of
the highest profile companies in the nation, including 50 of the Fortune 100.</p>
</body></html>
"""

SOLO_HOME = """
<html><head><title>Law Office of Jane Roe</title></head><body>
  <nav><a href="/">Home</a><a href="/our-attorney">Our Attorney</a><a href="/contact">Contact</a></nav>
  <p>I am Jane Roe, the founding attorney. My practice focuses on bicycle-accident law.</p>
</body></html>
"""

SWISSBIOLOGIC = """
<html><head><title>Swiss Biologic | Advanced Dental Products</title></head><body>
  <h1>Premium dental implants and biologic materials</h1>
  <p>We manufacture high-quality dental products for clinics worldwide.</p>
</body></html>
"""


# --- relevance gate -------------------------------------------------------


def test_relevance_gate_legal_vs_not():
    assert relevance_gate(Goetz_HOME).is_law_related
    assert relevance_gate(BIPC_ABOUT).is_law_related
    not_law = relevance_gate(SWISSBIOLOGIC)
    assert not not_law.is_law_related
    assert not_law.terms == []


def test_relevance_gate_jsonld_type_with_leading_space():
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type": " LegalService", "name": "X"}</script>'
        "<title>X</title></head><body>welcome</body></html>"
    )
    assert relevance_gate(html).is_law_related  # JSON-LD type wins even with thin text


# --- headcount cascade ----------------------------------------------------


def test_headcount_stated_big_h2_Goetz():
    hc, staff = extract_headcount([("home", Goetz_HOME)], base_url="https://x.com")
    assert hc.count == 40
    assert hc.is_min is True
    assert hc.method == "stated"
    assert staff == 600  # "600+ Staff"


def test_headcount_stated_prose_bipc():
    hc, _ = extract_headcount([("about", BIPC_ABOUT)], base_url="https://x.com")
    assert hc.count == 450
    assert hc.method == "stated"


def test_headcount_heading_roles_teplg():
    hc, staff = extract_headcount([("team", TEPLG_TEAM)], base_url="https://x.com")
    assert hc.method == "heading_roles"
    assert hc.count == 3  # Bill, Kirsten, Kathleen
    assert staff == 4  # Dawn, Kristen, Stephanie, Dianna


def test_headcount_profile_links():
    team = (
        "<html><body>"
        '<a href="/attorneys/jane-roe">Jane</a>'
        '<a href="/attorneys/john-doe">John</a>'
        '<a href="/attorneys/amy-poe">Amy</a>'
        '<a href="/about">About</a>'
        "</body></html>"
    )
    hc, _ = extract_headcount([("attorneys", team)], base_url="https://x.com")
    assert hc.method == "profile_links"
    assert hc.count == 3


def test_headcount_solo_signal():
    hc, _ = extract_headcount([("home", SOLO_HOME)], base_url="https://x.com")
    assert hc.count == 1
    assert hc.method == "solo"


def test_headcount_unknown_on_thin_page():
    hc, _ = extract_headcount([("home", "<html><body>Welcome</body></html>")], base_url="")
    assert hc.count is None
    assert hc.method == "unknown"


def test_headcount_drops_year_like_number():
    # "© 2023 ... Attorneys" must NOT be read as 2023 attorneys.
    html = "<html><body><p>Serving clients since 1998. © 2023 Smith Attorneys.</p></body></html>"
    hc, _ = extract_headcount([("home", html)], base_url="")
    assert hc.count != 2023


# --- years ----------------------------------------------------------------


def test_years_variants():
    assert extract_years("Over 25+ Years of experience")[0] == 25
    assert extract_years("Founded in 1850.", now_year=2026)[0] == 176
    assert extract_years("Serving for over a quarter century")[0] == 25
    assert extract_years("no temporal info here")[0] is None


# --- offices / phones / platform ------------------------------------------


def test_offices_from_footer_addresses():
    count, addrs = extract_offices(Goetz_HOME)
    cities = {a["city"] for a in addrs}
    assert {"Dallas", "Fort Worth", "Atlanta", "Chicago"} <= cities
    assert count == len(addrs) >= 4


def test_offices_stated_fallback_bipc():
    count, addrs = extract_offices(BIPC_ABOUT)
    assert addrs == []  # no footer addresses in the prose
    assert count == 16  # "16 offices" stated


def test_phones_normalized_and_deduped():
    html = '<html><body><a href="tel:18007770000">call</a> or (800) 777-0000</body></html>'
    phones = extract_phones(html)
    assert phones == ["+18007770000"]  # both forms collapse to one E.164


def test_detect_platform():
    assert detect_platform('<link href="/wp-content/themes/x.css">') == "wordpress"
    assert detect_platform('<img src="https://static.wixstatic.com/a.png">') == "wix"
    assert detect_platform('<img src="https://static1.squarespace.com/x">') == "squarespace"
    assert detect_platform("<html><body>plain</body></html>") == "custom"


# --- compose --------------------------------------------------------------


def test_extract_site_Goetz_composed():
    site = extract_site(
        [("home", Goetz_HOME)], base_url="https://Goetzlaw.com", now_year=2026
    )
    assert site.is_law_related
    assert site.attorney_count == 40
    assert site.attorney_count_is_min
    assert site.staff_count == 600
    assert site.years_in_operation == 25
    assert "recovered billions" in site.notable_signals
    assert site.url_verification_status == "verified"


def test_extract_site_not_a_law_firm_flagged():
    site = extract_site([("home", SWISSBIOLOGIC)], base_url="https://swissbiologic.com")
    assert not site.is_law_related
    assert site.url_verification_status == "not_a_law_firm"


def test_extract_site_empty_is_unreachable():
    site = extract_site([], base_url="")
    assert site.url_verification_status == "unreachable"
    assert site.needs_render
