"""No-network tests for the LLM entity-resolution adjudicator.

A stub decider stands in for Claude so the gating logic (link at/above the gold
threshold, reject otherwise, update the queue, never touch gold directly) is
tested deterministically."""

from __future__ import annotations

import duckdb
import pytest

from grocery_optimizer import config
from grocery_optimizer.db import init_db
from grocery_optimizer.silver.adjudicate import (
    AdjudicatorError,
    adjudicate_queue,
    make_claude_decider,
)


def _canonical(con, name, category=None):
    return con.execute(
        "INSERT INTO canonical_products (name, category) VALUES (?, ?) RETURNING canonical_id",
        [name, category],
    ).fetchone()[0]


def _queued_source(con, source, raw_name, candidate_id, proposed):
    spid = con.execute(
        """
        INSERT INTO source_products (source, source_sku, raw_name)
        VALUES (?, ?, ?) RETURNING source_product_id
        """,
        [source, raw_name, raw_name],
    ).fetchone()[0]
    con.execute(
        """
        INSERT INTO resolution_queue (source_product_id, candidate_canonical_id,
                                      proposed_confidence, status)
        VALUES (?, ?, ?, 'open')
        """,
        [spid, candidate_id, proposed],
    )
    return spid


def _stub_decide(source, candidate):
    name = source["raw_name"].lower()
    if "milk" in name:
        return {"same_product": True, "confidence": 0.95, "reason": "both whole milk"}
    if "borderline" in name:
        return {"same_product": True, "confidence": 0.80, "reason": "uncertain"}
    return {"same_product": False, "confidence": 0.2, "reason": "different product"}


def test_adjudicator_links_rejects_and_gates_on_threshold():
    con = duckdb.connect(":memory:")
    init_db(con)
    milk = _canonical(con, "Whole Milk", "Dairy")

    sp_milk = _queued_source(con, "kroger", "Store Whole Milk Gallon", milk, 0.82)
    sp_diff = _queued_source(con, "aldi_kcl", "Cheddar Cheese Block", milk, 0.79)
    sp_border = _queued_source(con, "wholefoods", "Borderline Item", milk, 0.80)

    stats = adjudicate_queue(con, _stub_decide)
    assert stats["processed"] == 3
    assert stats["linked"] == 1  # only the confident same-product
    assert stats["rejected"] == 2  # different + below-threshold same

    # Confident same-product is linked and now trusted (>= gold gate).
    row = con.execute(
        "SELECT canonical_id, match_method, match_confidence FROM source_products WHERE source_product_id = ?",
        [sp_milk],
    ).fetchone()
    assert row[0] == milk
    assert row[1] == "llm_adjudicator"
    assert row[2] >= 0.85

    # Different product: not linked, queue rejected.
    assert con.execute(
        "SELECT canonical_id FROM source_products WHERE source_product_id = ?", [sp_diff]
    ).fetchone()[0] is None

    # Same-product but confidence below the gold gate: NOT linked (never silently to gold).
    assert con.execute(
        "SELECT canonical_id FROM source_products WHERE source_product_id = ?", [sp_border]
    ).fetchone()[0] is None

    # No open rows remain; statuses recorded by the adjudicator.
    assert con.execute(
        "SELECT count(*) FROM resolution_queue WHERE status = 'open'"
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT count(*) FROM resolution_queue WHERE status = 'approved'"
    ).fetchone()[0] == 1


def test_adjudicator_respects_limit():
    con = duckdb.connect(":memory:")
    init_db(con)
    milk = _canonical(con, "Whole Milk")
    _queued_source(con, "kroger", "Whole Milk A", milk, 0.90)
    _queued_source(con, "wholefoods", "Whole Milk B", milk, 0.80)
    stats = adjudicate_queue(con, _stub_decide, limit=1)
    assert stats["processed"] == 1
    assert con.execute(
        "SELECT count(*) FROM resolution_queue WHERE status = 'open'"
    ).fetchone()[0] == 1


def test_make_decider_requires_api_key(monkeypatch):
    monkeypatch.setattr(config, "get_anthropic_api_key", lambda: None)
    with pytest.raises(AdjudicatorError):
        make_claude_decider(api_key=None)
