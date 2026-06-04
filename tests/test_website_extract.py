"""Tests for the firm-website extraction cascade.

Synthetic HTML fixtures model the real documented cases
(docs/data_sources/firm_websites.md §3/§4/§12): Goetz big-number H2s,
TEPLG team page, BIPC prose, Caritas stated count, a solo practice, and the
swissbiologic non-law site. Each pins one cascade branch.
"""

from __future__ import annotations

from legal_sourcing.enrichment.website_extract import (
    detect_platform,
    discover_internal_pages,
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


def test_headcount_ignores_phone_number_tail():
    # Noland case: a phone number's last group abuts "Lawyers" in the flattened
    # text ("...Call 478-621-4980 Lawyers in Macon") — must NOT yield 4980.
    home = (
        "<html><body><h1>Lawyers in Macon</h1>"
        "<p>Free consultation today. Call 478-621-4980 Lawyers in Macon, GA. "
        "Phone: 478-621-4980 Fax: 478-621-4982.</p></body></html>"
    )
    hc, _ = extract_headcount([("home", home)], base_url="https://x.com")
    assert hc.count != 4980
    assert hc.count is None  # nothing genuinely stated -> falls through to unknown


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


def test_offices_city_strips_street_tokens():
    # moreno.law / burgsimpson.com / hastingsfirm.com: street-suffix, unit, and
    # suite-letter tokens abut the city in the flattened footer text and must
    # not be kept as part of the city name.
    html = (
        "<html><body><footer>"
        "1901 Avenue of the Stars 2nd Floor Los Angeles, CA 90067 | "
        "4900 California Avenue Suite 210-B Bakersfield, CA 93309 | "
        "40 Inverness Drive East Englewood, CO 80112 | "
        "26503 Oak Ridge Dr The Woodlands, TX 77380"
        "</footer></body></html>"
    )
    count, addrs = extract_offices(html)
    cities = {a["city"] for a in addrs}
    assert "Los Angeles" in cities  # "2nd Floor" stripped
    assert "Bakersfield" in cities  # suite letter "B" stripped
    assert "The Woodlands" in cities  # "Dr" stripped, "The" kept
    assert "Englewood" in cities or "East Englewood" in cities  # "Drive" stripped
    # the exact pre-fix leaks must be gone
    assert {"Floor Los Angeles", "B Bakersfield", "Dr The Woodlands"} & cities == set()
    assert count == len(addrs)


def test_phones_normalized_and_deduped():
    html = '<html><body><a href="tel:18007770000">call</a> or (800) 777-0000</body></html>'
    phones = extract_phones(html)
    assert phones == ["+18007770000"]  # both forms collapse to one E.164


def test_detect_platform():
    assert detect_platform('<link href="/wp-content/themes/x.css">') == "wordpress"
    assert detect_platform('<img src="https://static.wixstatic.com/a.png">') == "wix"
    assert detect_platform('<img src="https://static1.squarespace.com/x">') == "squarespace"
    assert detect_platform("<html><body>plain</body></html>") == "custom"


def test_detect_platform_directory_profile_only_from_identity():
    # hastingsfirm regression: a firm's OWN site that links to its Avvo/Justia/
    # Martindale profiles (JSON-LD sameAs) is NOT a directory profile.
    firm = (
        '<html><head><link rel="canonical" href="https://hastingsfirm.com/">'
        '<script type="application/ld+json">{"@type":"Attorney","sameAs":'
        '["https://www.avvo.com/attorneys/x","https://lawyers.justia.com/lawyer/y",'
        '"https://www.martindale.com/attorney/z"]}</script></head>'
        '<body><link href="/wp-content/themes/x.css"></body></html>'
    )
    assert detect_platform(firm) == "wordpress"
    # A page whose OWN canonical/og:url is a directory domain IS a directory profile.
    prof = (
        '<html><head><link rel="canonical" '
        'href="https://lawyers.justia.com/lawyer/jane-roe"></head><body>x</body></html>'
    )
    assert detect_platform(prof) == "directory_profile"


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


# --- page discovery + multi-subpage aggregation ---------------------------


def test_discover_internal_pages():
    home = (
        "<html><body><nav>"
        '<a href="/about-us">About Us</a>'
        '<a href="/our-team">Our Team</a>'
        '<a href="/attorneys">Our Attorneys</a>'
        '<a href="/blog/post-1">Blog</a>'
        '<a href="https://twitter.com/firm">Twitter</a>'
        "</nav></body></html>"
    )
    pages = discover_internal_pages(home, "https://smithlaw.com")
    assert "https://smithlaw.com/about-us" in pages["about"]
    assert "https://smithlaw.com/our-team" in pages["team"]
    assert "https://smithlaw.com/attorneys" in pages["attorneys"]
    # blog + external links are excluded
    flat = [u for urls in pages.values() for u in urls]
    assert not any("/blog/" in u for u in flat)
    assert not any("twitter.com" in u for u in flat)


def test_headcount_aggregates_across_attorney_subpages():
    # Martin & Bonnett case: roster split across Partners / Associates pages.
    partners = '<html><body><a href="/attorneys/dan-bonnett">Dan</a><a href="/attorneys/susan-martin">Susan</a></body></html>'
    associates = '<html><body><a href="/attorneys/jane-roe">Jane</a><a href="/attorneys/dan-bonnett">Dan (dup)</a></body></html>'
    hc, _ = extract_headcount(
        [("attorneys", partners), ("attorneys", associates)], base_url="https://mb.com"
    )
    assert hc.method == "profile_links"
    assert hc.count == 3  # dan, susan, jane (dup collapses)


# --- announcement / press-release guard + gov flag (pilot findings) -------


def test_headcount_skips_announcement_headline():
    # Fennemore case: a merger headline must NOT be read as the firm total.
    news = (
        "<html><body><p>Fennemore expands in Northern California. "
        "15 Attorneys and Legal Professionals Join the firm.</p></body></html>"
    )
    hc, _ = extract_headcount([("home", news)], base_url="https://x.com")
    assert hc.count != 15


def test_headcount_keeps_real_stated_total():
    ok = (
        "<html><body><p>About us. With 240 attorneys in Louisiana and Texas, "
        "we serve clients.</p></body></html>"
    )
    hc, _ = extract_headcount([("home", ok)], base_url="https://x.com")
    assert hc.count == 240
    assert hc.method == "stated"


def test_extract_site_flags_gov_host():
    html = (
        "<html><head><title>Attorney General</title></head><body>"
        "<p>The Attorney General legal office serves the public with attorneys "
        "and counsel and litigation.</p></body></html>"
    )
    site = extract_site([("home", html)], base_url="https://www.azag.gov")
    assert site.url_verification_status == "government_or_edu"
