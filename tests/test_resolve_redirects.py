"""Tests for pipelines.resolve_redirects.

The `derive` path (redirect_domain from the crawler's resolved_url) is the
primary, no-network path and is covered end-to-end against a temp SQLite. The
active `fetch` path is exercised via `resolve_one` with a fake httpx client so no
real network call is made.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from legal_sourcing.models import WebsiteEnrichment
from legal_sourcing.models.base import Base
from legal_sourcing.pipelines import resolve_redirects as rr


def _engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'we.sqlite'}")
    Base.metadata.create_all(eng)
    return eng


def test_resolved_domain_from_url():
    assert rr.resolved_domain_from_url("https://www.taftlaw.com/about") == "taftlaw.com"
    assert rr.resolved_domain_from_url("http://smithlaw.com") == "smithlaw.com"
    assert rr.resolved_domain_from_url(None) is None
    assert rr.resolved_domain_from_url("not a url") is None


def test_derive_populates_redirect_domain(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    ts = datetime(2026, 6, 15, tzinfo=UTC)
    with Session(eng) as s:
        s.add_all(
            [
                # cross-domain redirect (acquisition): shermanhoward.com -> taftlaw.com
                WebsiteEnrichment(
                    website="shermanhoward.com",
                    resolved_url="https://www.taftlaw.com/",
                    enriched_at=ts,
                ),
                # resolves to itself (no cross-domain redirect)
                WebsiteEnrichment(website="smithlaw.com", resolved_url="https://smithlaw.com/home"),
                # never crawled -> no resolved_url -> stays NULL under derive
                WebsiteEnrichment(website="uncrawled.com", resolved_url=None),
            ]
        )
        s.commit()
    monkeypatch.setattr(rr, "make_engine", lambda: eng)

    counts = rr.derive()

    with Session(eng) as s:
        rows = {w.website: w.redirect_domain for w in s.scalars(select(WebsiteEnrichment)).all()}
    assert rows["shermanhoward.com"] == "taftlaw.com"
    assert rows["smithlaw.com"] == "smithlaw.com"
    assert rows["uncrawled.com"] is None
    assert counts["updated"] == 2
    assert counts["redirect"] == 1
    assert counts["same_site"] == 1
    assert counts["unresolved"] == 1


def test_derive_is_idempotent_and_respects_refresh(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    with Session(eng) as s:
        s.add(WebsiteEnrichment(website="a.com", resolved_url="https://b.com/"))
        s.commit()
    monkeypatch.setattr(rr, "make_engine", lambda: eng)

    first = rr.derive()
    assert first["updated"] == 1

    # second run: redirect_domain already set -> not re-scanned (NULL filter)
    second = rr.derive()
    assert second["scanned"] == 0
    assert second["updated"] == 0

    # if the crawler later changes resolved_url, --refresh re-derives it
    with Session(eng) as s:
        row = s.scalars(select(WebsiteEnrichment)).one()
        row.resolved_url = "https://c.com/"
        s.commit()
    refreshed = rr.derive(refresh=True)
    assert refreshed["updated"] == 1
    with Session(eng) as s:
        assert s.scalars(select(WebsiteEnrichment)).one().redirect_domain == "c.com"


def test_derive_dry_run_writes_nothing(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    with Session(eng) as s:
        s.add(WebsiteEnrichment(website="a.com", resolved_url="https://b.com/"))
        s.commit()
    monkeypatch.setattr(rr, "make_engine", lambda: eng)

    counts = rr.derive(dry_run=True)
    assert counts["redirect"] == 1
    assert counts["updated"] == 0
    with Session(eng) as s:
        assert s.scalars(select(WebsiteEnrichment)).one().redirect_domain is None


class _FakeResp:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakeClient:
    """Stand-in for httpx.Client: returns a stub response with the final (post-
    redirect) URL for known inputs, and raises like a dead host otherwise."""

    def __init__(self, final_by_url: dict[str, str]) -> None:
        self._final = final_by_url

    def head(self, url: str) -> _FakeResp:
        if url in self._final:
            return _FakeResp(self._final[url])
        raise httpx.ConnectError("no route to host")

    def get(self, url: str) -> _FakeResp:
        return self.head(url)


def test_resolve_one_follows_to_final_domain():
    client = _FakeClient({"https://shermanhoward.com": "https://www.taftlaw.com/"})
    assert rr.resolve_one("shermanhoward.com", client) == "taftlaw.com"


def test_resolve_one_unreachable_returns_none():
    client = _FakeClient({})  # every scheme/method raises
    assert rr.resolve_one("dead.example", client) is None
