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
    extract_contacts,
    extract_deactivation_status,
    extract_firm_descriptions,
    extract_firm_name,
    extract_headcount,
    extract_offices,
    extract_phones,
    extract_practice_areas,
    extract_site,
    extract_year_founded,
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
  <p>Swiss Biologic is a global manufacturer of premium dental implants,
  abutments, and biologic regeneration membranes for clinics and dental
  laboratories worldwide. Founded by materials scientists, we engineer titanium
  and ceramic components to exacting ISO 13485 standards. Our regenerative
  product line supports guided bone and tissue regeneration for implant
  dentistry. Surgeons and prosthodontists in more than forty countries rely on
  our biocompatible solutions, backed by a dedicated research and clinical
  support team that partners with dental schools on continuing education.</p>
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


def test_heading_roles_skips_testimonial_initials():
    # dmvinjurylaw.com: client reviews are headed "Firstname L." with "attorney"
    # in the review text — must NOT be counted as attorneys (only the real one).
    html = (
        "<html><body>"
        "<div><h3>Maria A.</h3><p>My attorney was amazing and fought for me.</p></div>"
        "<div><h3>Tony I.</h3><p>Best attorney I have ever met, highly recommend.</p></div>"
        "<div><h3>Eddy Z.</h3><p>The attorney and his whole team were great.</p></div>"
        "<div><h3>Jane Q. Whitfield</h3><p>Partner and Trial Attorney</p></div>"
        "</body></html>"
    )
    hc, _ = extract_headcount([("attorneys", html)], base_url="https://x.com")
    assert hc.method == "heading_roles"
    assert hc.count == 1  # only Jane Q. Whitfield


def test_heading_roles_dedups_same_attorney_across_formats():
    # wilshirelawfirm.com: the same attorney appears uppercase AND titlecase.
    html = (
        "<html><body>"
        "<div><h2>COLIN M. JONES, ESQ.</h2><p>Senior Attorney</p></div>"
        "<div><h2>Colin Jones, Esq.</h2><p>Partner and Attorney</p></div>"
        "<div><h2>RYAN CASEY, ESQ.</h2><p>Associate Attorney</p></div>"
        "</body></html>"
    )
    hc, _ = extract_headcount([("attorneys", html)], base_url="https://x.com")
    assert hc.count == 2  # Colin Jones (deduped) + Ryan Casey


def test_heading_roles_dedups_roster_across_pages():
    # llflegal.com: the same roster on multiple crawled pages must be counted by
    # distinct person, not summed per page (351 -> distinct).
    roster = (
        "<html><body>"
        "<div><h3>Donna M. Shaw</h3><p>Partner</p></div>"
        "<div><h3>Jeff Altshul</h3><p>Attorney</p></div>"
        "<div><h3>Bruce Craig</h3><p>Of Counsel</p></div>"
        "</body></html>"
    )
    hc, _ = extract_headcount([("attorneys", roster), ("team", roster)], base_url="https://x.com")
    assert hc.count == 3  # not 6


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


def test_years_skips_combined_experience():
    # "X years of combined/collective experience" is summed across the team, not
    # the firm's age (thevirgalawfirm.com / sdtriallaw.com).
    assert extract_years("Over 100 years of combined experience")[0] != 100
    assert extract_years("100 years + of collective legal mastery")[0] != 100
    # a genuine firm-age statement still works
    assert extract_years("Serving clients for 30 years")[0] == 30


def test_years_rejects_prehistoric_founding():
    # missourilawyers case: "Founded in 1764 by French settlers" is St. Louis
    # city history, not the firm (no US firm predates ~1790).
    assert (
        extract_years("...in North America. Founded in 1764 by French.", now_year=2026)[0] is None
    )
    # a genuinely old firm (Cadwalader, 1792) is still accepted
    assert extract_years("Established in 1850.", now_year=2026)[0] == 176


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


def test_extract_site_thin_js_page_not_flagged_not_a_law_firm():
    # beaverlawoffice.com (0 chars) / dankolawllc.com (114): a near-empty JS
    # shell with no legal tokens must be unverified + needs_render, NOT the
    # confident not_a_law_firm (we never actually read the page).
    site = extract_site(
        [("home", "<html><body><div></div></body></html>")],
        base_url="https://beaverlawoffice.com",
    )
    assert site.needs_render
    assert not site.is_law_related
    assert site.url_verification_status == "unverified"


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


def test_headcount_profile_links_counted_on_home_page():
    # peterferracuti case: attorney-profile links live on the HOME page and no
    # separate /attorneys index was discovered -> must still be counted, not
    # ignored as "not a team page" (previously fell through to unknown).
    home = (
        "<html><body><footer>"
        '<a href="/attorney/dunn-travis">Travis Dunn</a>'
        '<a href="/attorney/ferracuti-alexis-p">Alexis Ferracuti</a>'
        '<a href="/attorney/ludwinski-matthew">Matthew Ludwinski</a>'
        "</footer></body></html>"
    )
    hc, _ = extract_headcount([("home", home)], base_url="https://x.com")
    assert hc.method == "profile_links"
    assert hc.count == 3


# --- announcement / press-release guard + gov flag (pilot findings) -------


def test_headcount_skips_announcement_headline():
    # Fennemore case: a merger headline must NOT be read as the firm total.
    news = (
        "<html><body><p>Fennemore expands in Northern California. "
        "15 Attorneys and Legal Professionals Join the firm.</p></body></html>"
    )
    hc, _ = extract_headcount([("home", news)], base_url="https://x.com")
    assert hc.count != 15


def test_headcount_skips_ordinal_section_number():
    # burnerlaw case: a zero-padded section/ordinal marker ("...02 Attorneys
    # Mentorship...") must NOT be read as 2 attorneys (leading-zero guard).
    html = (
        "<html><body><p>01 AI for Legal Practice 02 Attorneys Mentorship "
        "and Development 03 Community</p></body></html>"
    )
    hc, _ = extract_headcount([("home", html)], base_url="https://x.com")
    assert hc.count is None


def test_headcount_keeps_real_stated_total():
    ok = (
        "<html><body><p>About us. With 240 attorneys in Louisiana and Texas, "
        "we serve clients.</p></body></html>"
    )
    hc, _ = extract_headcount([("home", ok)], base_url="https://x.com")
    assert hc.count == 240
    assert hc.method == "stated"


def test_headcount_caps_statewide_population_stat():
    # criminaldefenseteam case: "More than 15,000 lawyers are practicing in
    # Indiana" is a bar-population stat, not this firm's headcount.
    html = (
        "<html><body><p>Criminal Trial Specialists. More than 15,000 lawyers "
        "are practicing in Indiana, but few focus on criminal defense.</p></body></html>"
    )
    hc, _ = extract_headcount([("home", html)], base_url="https://x.com")
    assert hc.count != 15000


def test_headcount_skips_population_there_are():
    # vansantlaw case: "there are approximately 4000 lawyers throughout the
    # nation" is a population stat (firm has 5), not a headcount.
    html = (
        "<html><body><p>Since 1993, there are approximately 4000 lawyers "
        "throughout the nation who handle these cases.</p></body></html>"
    )
    hc, _ = extract_headcount([("home", html)], base_url="https://x.com")
    assert hc.count != 4000


def test_headcount_skips_award_quota_per_state():
    # vansantlaw case: "Top 40 Under 40 is restricted to only 40 attorneys per
    # state" is an award quota, not the firm's headcount.
    html = (
        "<html><body><p>The Top 40 Under 40 is restricted to only 40 attorneys "
        "per state.</p></body></html>"
    )
    hc, _ = extract_headcount([("home", html)], base_url="https://x.com")
    assert hc.count != 40


def test_headcount_skips_top_n_award():
    # petrellilaw case: "The National Advocates Top 100 Lawyers" is an award,
    # not a firm headcount.
    html = (
        "<html><body><p>Recognized for the National Advocates Top 100 Lawyers "
        "list every year.</p></body></html>"
    )
    hc, _ = extract_headcount([("home", html)], base_url="https://x.com")
    assert hc.count != 100


PRACTICE_HOME = """
<html><head><title>Smith Law | Personal Injury Lawyers</title></head><body>
<nav>
  <a href="/about">About Us</a>
  <a href="/practice-areas/personal-injury">Personal Injury</a>
  <a href="/practice-areas/family-law">Family Law</a>
  <a href="/practice-areas/car-accidents">Car Accidents</a>
  <a href="/locations/phoenix">Phoenix</a>
  <a href="/contact">Contact</a>
</nav>
<footer>123 Main St Suite 200 Phoenix, AZ 85016</footer>
</body></html>
"""


def test_extract_firm_name():
    # legal JSON-LD `name` is trusted (preferred over the descriptor title).
    jsonld = (
        "<html><head><title>Personal Injury Lawyers | Smith &amp; Jones</title>"
        '<script type="application/ld+json">'
        '{"@type":"Attorney","name":"Smith & Jones LLP"}</script>'
        "</head><body></body></html>"
    )
    raw, norm = extract_firm_name([("home", jsonld)])
    assert raw == "Smith & Jones LLP"
    assert norm == "smith jones"
    # title-only: the firm segment (strong marker) beats the descriptor segment.
    title_only = (
        "<html><head><title>Personal Injury Lawyers | Hensley Legal Group</title>"
        "</head><body></body></html>"
    )
    assert extract_firm_name([("home", title_only)])[0] == "Hensley Legal Group"
    # a pure practice descriptor is NOT mistaken for a firm name.
    desc = "<html><head><title>Phoenix Personal Injury Lawyers</title></head><body></body></html>"
    assert extract_firm_name([("home", desc)]) == (None, None)


def test_extract_firm_name_rejects_generic_descriptors():
    # The SEO descriptor segment ("Phoenix Law Firm") must lose to the real name
    # later in the title via its entity suffix (Alex's flagged cfmlaw/treonshook bug).
    t = "<html><head><title>Phoenix Law Firm | Treon &amp; Shook, PLLC</title></head><body></body></html>"
    assert extract_firm_name([("home", t)])[0] == "Treon & Shook, PLLC"

    # JSON-LD LocalBusiness/Organization name is now trusted (treonshook's real name
    # lived only under @type LocalBusiness, so it was being ignored).
    lb = (
        "<html><head><title>Phoenix Law Firm</title>"
        '<script type="application/ld+json">'
        '{"@type":"LocalBusiness","name":"Treon & Shook, PLLC"}</script>'
        "</head><body></body></html>"
    )
    assert extract_firm_name([("home", lb)])[0] == "Treon & Shook, PLLC"

    # Domain-consistency picks the real name over a co-occurring practice descriptor.
    og = (
        '<html><head><meta property="og:site_name" '
        'content="Fielding Law | Personal Injury Law Firm"></head><body></body></html>'
    )
    assert extract_firm_name([("home", og)], base_url="https://fieldinglawfirm.com")[0] == "Fielding Law"

    # Pure-generic + practice-area descriptors are NEVER a firm identity.
    for desc in ("Law Firm", "Legal Services", "Personal Injury Law Firm", "Immigration Law Firm"):
        html = f"<html><head><title>{desc}</title></head><body></body></html>"
        assert extract_firm_name([("home", html)]) == (None, None), desc

    # Site-builder / domain-parking placeholders are not names.
    for ph in ("HugeDomains.com", "mysite 1", "IM Template FL2"):
        html = f"<html><head><title>{ph}</title></head><body></body></html>"
        assert extract_firm_name([("home", html)]) == (None, None), ph

    # A real surname firm whose name echoes its domain is KEPT (no over-correction).
    sm = "<html><head><title>Personal Injury Lawyers | Smith Law Firm</title></head><body></body></html>"
    assert (
        extract_firm_name([("home", sm)], base_url="https://smithlawfirm.com")[0] == "Smith Law Firm"
    )


def test_extract_contacts():
    # TEPLG team page: the 3 attorneys become contacts with titles; staff
    # (paralegal/assistant/coordinator) are excluded, same as the headcount.
    contacts = extract_contacts([("team", TEPLG_TEAM)])
    assert [c["name_raw"] for c in contacts] == ["Bill Deitch", "Kirsten Izatt", "Kathleen DiCola"]
    assert contacts[0]["name_normalized"] == "bill deitch"
    assert all("attorney" in (c["title"] or "").lower() for c in contacts)


def test_extract_contacts_excludes_testimonial_authors():
    # client-review headings "Firstname L." are not contacts (dmvinjurylaw).
    html = (
        "<html><body>"
        "<div><h3>Maria A.</h3><p>My attorney was great.</p></div>"
        "<div><h3>Jane Q. Whitfield</h3><p>Partner and Attorney</p></div>"
        "</body></html>"
    )
    contacts = extract_contacts([("attorneys", html)])
    assert [c["name_raw"] for c in contacts] == ["Jane Q. Whitfield"]


def test_extract_deactivation_status():
    assert (
        extract_deactivation_status("We're getting things ready. This won't take long.") == "parked"
    )
    assert extract_deactivation_status("This firm has permanently closed its doors.") == "closed"
    assert extract_deactivation_status("Our attorneys proudly serve clients in Phoenix.") is None


def test_extract_year_founded():
    assert extract_year_founded("Established in 1947, the firm grew.", now_year=2026) == 1947
    assert extract_year_founded("Serving clients since 1850.", now_year=2026) == 1850
    # pre-1780 (city/historical reference) is rejected; bare "N years" is not a year
    assert (
        extract_year_founded("...North America. Founded in 1764 by French.", now_year=2026) is None
    )
    assert extract_year_founded("Over 25 years of experience") is None


def test_extract_site_populates_name_year_postal():
    html = (
        "<html><head><title>Smith &amp; Jones, LLP | Attorneys</title>"
        '<meta property="og:site_name" content="Smith &amp; Jones, LLP"></head><body>'
        "<p>Established in 1990, our firm serves clients statewide.</p>"
        "<footer>123 Main St, Phoenix, AZ 85016</footer></body></html>"
    )
    site = extract_site([("home", html)], base_url="https://smithjones.com", now_year=2026)
    assert site.name_raw == "Smith & Jones, LLP"
    assert site.name_normalized == "smith jones"
    assert site.year_founded == 1990
    assert site.primary_city == "Phoenix"
    assert site.primary_postal_code == "85016"


def test_extract_firm_descriptions_sections():
    # About page split into {heading, text} sections; nav/listing/boilerplate
    # headings (Practice Areas, Newsletter) dropped with their body copy.
    about = (
        "<html><body>"
        "<h1>About Our Firm</h1>"
        "<p>Founded in 1985, Smith &amp; Jones has represented injured clients "
        "across the state for nearly four decades with a focus on results.</p>"
        "<h2>Our Approach</h2>"
        "<p>We treat every client like family and prepare every case as if it "
        "will go to trial, which is how we consistently earn top recoveries.</p>"
        "<h2>Practice Areas</h2>"
        "<p>Personal injury, car accidents, wrongful death, and so much more.</p>"
        "<h2>Newsletter</h2>"
        "<p>Subscribe to our newsletter for the latest firm updates each month.</p>"
        "</body></html>"
    )
    secs = extract_firm_descriptions([("about", about), ("home", "<p>ignored</p>")])
    assert [s["heading"] for s in secs] == ["About Our Firm", "Our Approach"]
    assert all(len(s["text"]) >= 60 for s in secs)
    assert "1985" in secs[0]["text"]


def test_extract_firm_descriptions_leading_copy_no_heading():
    home = (
        "<html><body>"
        "<p>Our boutique firm has counseled startups and founders on venture "
        "financing, mergers, and intellectual property for over twenty years.</p>"
        "</body></html>"
    )
    secs = extract_firm_descriptions([("home", home)])
    assert len(secs) == 1
    assert secs[0]["heading"] is None
    assert "boutique firm" in secs[0]["text"]


def test_extract_practice_areas_matches_taxonomy_not_location():
    slugs, raw, _ = extract_practice_areas([("home", PRACTICE_HOME)], base_url="https://smithlaw.com")
    assert set(slugs) == {"personal-injury", "family-law", "auto-accidents"}
    # location / nav links never become practice areas (taxonomy is the firewall)
    assert "phoenix" not in slugs
    assert "Personal Injury" in raw


def test_extract_practice_areas_from_path_slug():
    # icon-only link (no anchor text) -> slug comes from the /practice-areas/ path
    home = (
        '<html><body><a href="/practice-areas/wrongful-death"><img src="x.png"></a></body></html>'
    )
    slugs, _, _ = extract_practice_areas([("home", home)], base_url="https://x.com")
    assert "wrongful-death" in slugs


def test_extract_practice_areas_unmatched_from_url():
    # A practice area the firm DECLARES via a /practice-areas/{slug} URL but the
    # taxonomy doesn't recognize is captured as `unmatched`; the section landing
    # page and ordinary nav anchors are NOT (URL-declared only, never anchor text).
    home = (
        "<html><body>"
        '<a href="/practice-areas/equine-law">Equine Law</a>'
        '<a href="/practice-areas/">Practice Areas</a>'
        '<a href="/about-us">About Us</a>'
        '<a href="/practice-areas/personal-injury">Personal Injury</a>'
        "</body></html>"
    )
    slugs, _, unmatched = extract_practice_areas([("home", home)], base_url="https://x.com")
    assert "personal-injury" in slugs  # known area still matches
    assert unmatched == ["equine law"]  # URL-declared unknown area, normalized


def test_malformed_bracket_href_does_not_crash_extraction():
    # Python 3.14's urljoin RAISES ValueError ("Invalid IPv6 URL") on a stray
    # bracket in an href — and that happens before safe_urlparse can run. A single
    # junk href must not abort a whole site's extraction (this crashed the full
    # FSR load at firm #3765); safe_urljoin swallows it and skips just that link.
    html = (
        "<html><body>"
        '<a href="http://[">junk</a>'
        '<a href="//[::1">junk2</a>'
        '<a href="/practice-areas/family-law">Family Law</a>'
        "</body></html>"
    )
    slugs, _, _ = extract_practice_areas([("home", html)], base_url="https://x.com")
    assert "family-law" in slugs  # good href parsed; malformed ones skipped, no raise
    assert extract_site([("home", html)], base_url="https://x.com") is not None


def test_extract_site_separates_office_location_from_practice_areas():
    # The user's constraint: office LOCATION (where the firm sits) must not be
    # confused with PRACTICE AREAS (what it does). Phoenix is the office city,
    # never a practice area; personal-injury is a practice area, never a place.
    site = extract_site([("home", PRACTICE_HOME)], base_url="https://smithlaw.com")
    assert site.primary_city == "Phoenix"
    assert site.primary_state == "AZ"
    assert "personal-injury" in site.practice_areas
    assert "phoenix" not in [s.lower() for s in site.practice_areas]


def test_headcount_rejects_non_firm_stated_counts():
    # Real full-run false positives: fees, phone tails, professional networks,
    # bar/cert populations, statistics, other-firm mentions — none are THIS
    # firm's own headcount, so the stated count must not be read from them.
    cases = [
        ("A portion of the $5000 attorney flat fee is usually paid", 5000),
        ("DLA Piper has 4,827 attorneys, making it the second largest", 4827),
        ("Florida Bar members, approximately 4,800 lawyers, are board certified", 4800),
        ("a network representing 90 firms and 4,500 lawyers in 60 countries", 4500),
        ("provides access to more than 4,500 lawyers worldwide via mackrell", 4500),
        ("call us (401) 288 - 3888 attorney on call 24/7", 3888),
        ("Matters Handled 3,500+ Lawyers Trained 5,000+ Courts Admitted", 3500),
        ("Advanced Filter 2238 Lawyers Found Clear All filters", 2238),
        ("50+ law firms with close to 3,000 lawyers practicing nationwide", 3000),
        ("an academy that is limited to 250 attorneys in the country", 250),
        ("Fewer than 1,600 attorneys nationwide are fellows of this society", 1600),
        ("Court filing fees: $700 - $1,600 Attorney fees apply here", 1600),
        ("Avvo rating and endorsed by 1,700 lawyers we will review your case", 1700),
        # frequency-spike false positives (count clustered across many firms):
        ("Phoenix office info Open 24/7 Attorney Advertising disclaimer", 7),  # arizonazitonlaw
        (
            "San Diego Chapter 7 Attorneys handle Chapter 7 Bankruptcy filings",
            7,
        ),  # bankruptcyattorneys
        (
            "Susan: After interviewing 10+ attorneys, I was glad to find them",
            10,
        ),  # adlerandadler (review)
        ("Google review: 10/10 Lawyer! Jarom is responsive and helpful", 10),  # bangerterlawfirm
        ("DUI defense. Voted as one of The Best 100 Lawyers in America", 100),  # dmcantor (award)
        (
            "It's personal to us. We don't have 100 attorneys here, you matter",
            100,
        ),  # barteltlaw (negation)
        (
            "He is one of approximately 100 attorneys in Ohio to have earned this",
            100,
        ),  # cornwell-law (cert)
        (
            "Big firms in Denver have teams of 100+ lawyers and staff; we differ",
            100,
        ),  # coloradopersonalinjuryhelp
    ]
    for phrase, bad in cases:
        html = f"<html><body><p>{phrase}</p></body></html>"
        hc, _ = extract_headcount([("home", html)], base_url="https://x.com")
        assert hc.count != bad, f"should reject {bad} from {phrase!r} (got {hc.count})"


def test_headcount_keeps_real_big_firm_stated_counts():
    # Real AmLaw / large-firm self-counts that MUST survive the guards above.
    cases = [
        ("Approximately 4,000 Attorneys Firmwide Meet our lawyers", 4000),
        ("our team of more than 2,200 lawyers and legal professionals", 2200),
        ("approximately 2,200 attorneys practicing in over 250 areas", 2200),
        ("With over 2,400 lawyers across 60 offices worldwide today", 2400),
        ("1,250 attorneys strong. Taft Fellowship Scholars program", 1250),
        ("Jackson Lewis P.C. 1,100+ attorneys located in major cities nationwide", 1100),
        ("With 240 attorneys in Louisiana and Texas, we serve clients", 240),
        ("With 35 years, 1,000 attorneys, and $30 billion recovered for clients", 1000),
        ("Our 475 attorneys and government relations professionals serve", 475),
        # small/mid firms whose real count must survive the spike guards above:
        ("practice areas with the help of our 7 attorneys and 30+ staff", 7),  # avardlaw
        (
            "The firm now has a total of 7 Attorneys, 20 support staff and 4 offices",
            7,
        ),  # brookslawgroup
        (
            "intellectual property law firms, our 100 attorneys and agents provide",
            100,
        ),  # cantorcolburn
        (
            "Our entire firm 10 attorneys and 35+ legal professionals serve clients",
            10,
        ),  # 877kajycares
        (
            "the size of our firm 10 lawyers offers you the best choice for your case",
            10,
        ),  # utahattorneys
    ]
    for phrase, good in cases:
        html = f"<html><body><p>{phrase}</p></body></html>"
        hc, _ = extract_headcount([("home", html)], base_url="https://x.com")
        assert hc.count == good, f"should keep {good} from {phrase!r} (got {hc.count}/{hc.method})"
        assert hc.method == "stated"


def test_extract_site_flags_gov_host():
    html = (
        "<html><head><title>Attorney General</title></head><body>"
        "<p>The Attorney General legal office serves the public with attorneys "
        "and counsel and litigation.</p></body></html>"
    )
    site = extract_site([("home", html)], base_url="https://www.azag.gov")
    assert site.url_verification_status == "government_or_edu"
