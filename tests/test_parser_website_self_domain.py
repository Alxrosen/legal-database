"""Every directory parser must refuse to record the directory's OWN
domain as a firm's website.

Because `normalize_url` collapses a URL to its bare domain, a single
leaked self-domain (e.g. `findlaw.com`) would be shared by every firm
that leaked it, fabricating website matches across unrelated firms in
the resolution layer. These tests pin the guard for the shared helper
and for each source parser (az_bar, martindale, findlaw, justia).
"""

from __future__ import annotations

from legal_sourcing.normalize.url import strip_self_domain
from legal_sourcing.parsers.az_bar import _record_to_firm_dict
from legal_sourcing.parsers.findlaw import FindLawCityParser
from legal_sourcing.parsers.justia import JustiaDirectoryParser
from legal_sourcing.parsers.martindale import parse_firm_profile

# ---- shared helper --------------------------------------------------------


def test_strip_self_domain_matches_domain_and_subdomains():
    own = ("findlaw.com",)
    assert strip_self_domain("https://www.findlaw.com/x", own) is None
    assert strip_self_domain("https://lawyers.findlaw.com/y", own) is None
    assert strip_self_domain("http://FINDLAW.COM", own) is None
    # A real firm site is kept; a look-alike domain is NOT a subdomain.
    assert strip_self_domain("https://smithlaw.com", own) == "https://smithlaw.com"
    assert strip_self_domain("https://myfindlaw.com", own) == "https://myfindlaw.com"
    assert strip_self_domain(None, own) is None
    assert strip_self_domain("", own) is None


# ---- AZ Bar (JSON FirmURL) ------------------------------------------------


def _azbar(firm_url):
    rec = {"FirstName": "Jane", "LastName": "Doe", "Company": "Doe Law", "FirmURL": firm_url}
    return _record_to_firm_dict(rec, "https://api-proxy.azbar.org/x")["website_raw"]


def test_az_bar_drops_own_domain():
    assert _azbar("https://www.azbar.org/members/123") is None
    assert _azbar("https://doelaw.com") == "https://doelaw.com"


# ---- Martindale (profile website button) ----------------------------------


def _martindale(href):
    html = f'<html><body><a class="webstats-website-click" href="{href}">Website</a></body></html>'
    return parse_firm_profile(html.encode()).get("firm_website_url")


def test_martindale_drops_own_domain():
    assert _martindale("https://www.martindale.com/firm/acme") is None
    assert _martindale("https://acmelaw.com") == "https://acmelaw.com"


# ---- FindLaw (SRP card website button) ------------------------------------


def _findlaw(href):
    html = f"""<html><body>
      <div class="fl-serp-card organic">
        <a class="fl-serp-card-title" href="/personal-injury/arizona/phoenix/acme-NDk">Acme Law</a>
        <a data-testid="website-button-link" href="{href}">Website</a>
      </div>
    </body></html>"""
    recs = FindLawCityParser().parse_bytes(
        html.encode(),
        source_url="https://lawyers.findlaw.com/personal-injury/arizona/phoenix/",
    )
    return recs[0]["website_raw"]


def test_findlaw_drops_own_domain():
    assert _findlaw("https://www.findlaw.com/firm/acme") is None
    assert _findlaw("https://acmelaw.com") == "https://acmelaw.com"


# ---- Justia (card website link) -------------------------------------------


def _justia(href):
    html = f"""<html><body>
      <div class="jld-card -organic" data-vars-profile="9">
        <strong class="name"><a href="https://lawyers.justia.com/lawyer/x-9">X</a></strong>
        <a class="website" href="{href}">View Website</a>
      </div>
    </body></html>"""
    recs = JustiaDirectoryParser().parse_bytes(html.encode(), source_url="x")
    return recs[0]["website_raw"]


def test_justia_drops_own_domains():
    assert _justia("https://justia.lawyer/x-9") is None
    assert _justia("https://lawyers.justia.com/lawyer/x-9") is None
    assert _justia("https://xlaw.com") == "https://xlaw.com"
