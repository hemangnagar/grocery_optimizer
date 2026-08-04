"""Tests for the weekly narrator (agentic layer #3): facts, audit gate, serving."""

from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient

from grocery_optimizer import config
from grocery_optimizer.api.app import create_app
from grocery_optimizer.db import init_db
from grocery_optimizer.gold.narrator import (
    NarrationAuditError,
    audit_narration,
    build_facts,
    narrate,
)
from grocery_optimizer.scripts.seed_demo import seed_demo


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    init_db(c)
    info = seed_demo(c)
    c.execute("SET VARIABLE basket_id = ?", [info["basket_id"]])
    yield c
    c.close()


def _basket_id(con) -> int:
    return con.execute("SELECT getvariable('basket_id')").fetchone()[0]


# ---- fact pack -------------------------------------------------------------

def test_facts_match_the_verdict(con):
    facts = build_facts(con, _basket_id(con))
    assert facts["winner"]["store"] == "Harris Teeter / Kroger"
    assert facts["winner"]["total"] == 123.17
    assert facts["runner_up"]["store"] == "Whole Foods Market"
    assert facts["saving_vs_runner_up"] == pytest.approx(46.10)
    assert facts["split"]["extra_saving"] == pytest.approx(23.84)
    aldi = next(s for s in facts["other_stores"] if s["store"] == "Aldi")
    assert aldi["missing_count"] == 3
    # provenance: every line's gold price row is cited
    assert len(facts["_provenance"]["price_ids"]) > 0
    assert facts["_provenance"]["basket_id"] == _basket_id(con)


# ---- audit gate ------------------------------------------------------------

def test_audit_accepts_fact_pack_numbers(con):
    facts = build_facts(con, _basket_id(con))
    text = (
        "Shop at Harris Teeter this week: $123.17 for all 26 items. "
        "Whole Foods would run $169.27, so you save $46.10. Aldi looks cheap "
        "at $83.48 but is missing 3 of 26 items. A perfect 3-store split "
        "would total $99.33 — another $23.84 if you want the extra stops."
    )
    assert audit_narration(text, facts) == []


def test_audit_rejects_invented_numbers(con):
    facts = build_facts(con, _basket_id(con))
    assert audit_narration("You save $999.99 this week!", facts)
    assert audit_narration("Covers 27 items", facts)
    # dates are not treated as numbers
    assert audit_narration("For the ad week of 2026-07-29, pay $123.17.", facts) == []


def test_narrate_raises_and_stores_nothing_on_bad_number(con):
    with pytest.raises(NarrationAuditError):
        narrate(con, _basket_id(con), lambda facts: "Everything is $1.99!")
    assert con.execute("SELECT count(*) FROM narrations").fetchone()[0] == 0


def test_narrate_stores_audited_narration_with_citations(con):
    def write(facts):
        return (
            f"{facts['winner']['store']} wins at ${facts['winner']['total']:.2f} "
            f"covering all {facts['winner']['n_items']} items — "
            f"${facts['saving_vs_runner_up']:.2f} less than "
            f"{facts['runner_up']['store']}."
        )

    result = narrate(con, _basket_id(con), write, model="stub")
    assert result["audit_ok"] is True
    assert "price_id" in result["citations"]
    stored = con.execute(
        "SELECT narration, citations, audit_ok, model FROM narrations"
    ).fetchone()
    assert stored[0] == write(build_facts(con, _basket_id(con)))
    assert stored[2] is True and stored[3] == "stub"


# ---- serving ---------------------------------------------------------------

@pytest.fixture()
def served(tmp_path, monkeypatch):
    db_path = tmp_path / "narr.duckdb"
    c = duckdb.connect(str(db_path))
    init_db(c)
    info = seed_demo(c)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    return c, info["basket_id"]


def test_api_serves_only_stored_narrations(served):
    con, basket_id = served
    client = TestClient(create_app())
    con.close()  # release write lock for the API's read-only connections
    assert client.get("/api/narrative").status_code == 404  # nothing stored yet


def test_api_returns_stored_narration(served):
    con, basket_id = served
    narrate(con, basket_id, lambda f: f"Winner at ${f['winner']['total']:.2f}.",
            model="stub")
    con.close()
    client = TestClient(create_app())
    body = client.get("/api/narrative").json()
    assert body["narration"] == "Winner at $123.17."
    assert "gold price rows" in body["citations"]
    assert body["model"] == "stub"
