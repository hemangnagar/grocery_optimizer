"""No-network tests for step 3: Lidl overview parser, KCL Aldi parser, and the
polite/robots behavior of the scraping clients. Uses committed HTML fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from grocery_optimizer.bronze.fetcher import DEFAULT_USER_AGENT
from grocery_optimizer.bronze.kcl import KCLRobotsError, _assert_allowed
from grocery_optimizer.silver.kcl import parse_deals
from grocery_optimizer.silver.lidl import parse_overview

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_kcl_deals():
    html = (FIXTURES / "kcl_aldi_sample.html").read_bytes()
    deals = parse_deals(html)
    assert len(deals) == 2  # store node skipped, two products parsed

    by_name = {d["name"]: d for d in deals}
    cuke = by_name["Cucumber"]
    assert cuke["price"] == 0.45
    assert cuke["regular_price"] == 0.75
    assert cuke["discount_pct"] == 40
    assert cuke["source_sku"] == "3253345"  # Aldi SKU from URL
    assert cuke["kcl_id"] == "prodCuc"
    assert cuke["store"] == "aldi"


def test_parse_kcl_handles_missing_regular_price():
    html = (FIXTURES / "kcl_aldi_sample.html").read_bytes()
    deals = parse_deals(html)
    mand = next(d for d in deals if d["name"].startswith("Mandarins"))
    assert mand["price"] == 2.99
    assert mand["regular_price"] is None
    assert mand["discount_pct"] is None
    assert mand["source_sku"] == "999123"


def test_parse_lidl_overview():
    html = (FIXTURES / "lidl_overview_sample.html").read_bytes()
    flyers = parse_overview(html)
    assert len(flyers) == 2  # non-flyer link ignored

    first = flyers[0]
    assert first["detail_url"].startswith("https://www.lidl.com/flyer/esi-flyer/")
    assert first["valid_from"] == "7/8/2026"
    assert first["valid_to"] == "7/14/2026"

    # Second flyer's title comes from the <img alt> since the link has no text.
    second = flyers[1]
    assert "7/15/2026" in second["title"]
    assert second["valid_to"] == "7/21/2026"


def test_kcl_robots_guard():
    # Disallowed prefixes and tracking params must be rejected.
    for bad in ("/api/deals", "/tag/aldi", "/search?q=x", "/webview/x"):
        with pytest.raises(KCLRobotsError):
            _assert_allowed(bad)
    with pytest.raises(KCLRobotsError):
        _assert_allowed("/coupons-for/aldi?kclt=123")
    # Allowed path does not raise.
    _assert_allowed("/coupons-for/aldi")


def test_honest_user_agent():
    # We identify ourselves and do not impersonate a browser or GPTBot.
    assert "grocery-optimizer" in DEFAULT_USER_AGENT
    assert "github.com/hemangnagar" in DEFAULT_USER_AGENT
