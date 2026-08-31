"""The ODCS contract is enforceable, not documentation: these tests hold the
live gold schema (and its quality rules) to contracts/gold_current_prices.odcs.yaml,
and the OpenLineage RunEvent to its spec shape."""

from __future__ import annotations

import duckdb
import pytest

from grocery_optimizer.db import init_db
from grocery_optimizer.gold import lineage
from grocery_optimizer.gold.contract import load_contract, verify_contract


def _fresh():
    con = duckdb.connect(":memory:")
    init_db(con)
    return con


def test_contract_matches_live_gold_schema():
    con = _fresh()
    assert verify_contract(con) == []


def test_contract_catches_schema_drift(tmp_path):
    contract = load_contract()
    # Simulate drift both ways: contract promises a column the view lost, and
    # the view grows a column the contract doesn't know.
    props = contract["schema"][0]["properties"]
    renamed = [dict(p) for p in props]
    renamed[0] = dict(renamed[0], name="canonical_identifier")
    contract["schema"][0]["properties"] = renamed

    import yaml

    drifted = tmp_path / "drifted.yaml"
    drifted.write_text(yaml.safe_dump(contract), encoding="utf-8")

    violations = verify_contract(_fresh(), drifted)
    assert any("canonical_identifier" in v and "missing" in v for v in violations)
    assert any("canonical_id" in v and "not in the contract" in v for v in violations)


def test_unknown_quality_rule_is_itself_a_violation(tmp_path):
    contract = load_contract()
    contract["quality"].append({"rule": "vibes", "dimension": "accuracy"})
    import yaml

    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    violations = verify_contract(_fresh(), path)
    assert any("unknown quality rule 'vibes'" in v for v in violations)


def test_openlineage_event_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(lineage, "LINEAGE_DIR", tmp_path)
    monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
    con = _fresh()
    con.execute(
        "INSERT INTO bronze_manifest (source, source_kind, fetched_at, raw_path) "
        "VALUES ('kroger', 'api', CURRENT_TIMESTAMP, 'x.json')"
    )
    event, delivery = lineage.emit_lineage(con, "normalize", {"prices": 3, "skip": "no"})

    assert delivery is None  # no collector configured -> file only
    assert event["eventType"] == "COMPLETE"
    assert event["schemaURL"].startswith("https://openlineage.io/spec/")
    assert event["job"] == {"namespace": "grocery_optimizer", "name": "normalize"}
    assert {i["name"] for i in event["inputs"]} == {"kroger"}
    out_names = {o["name"] for o in event["outputs"]}
    assert "gold_current_prices" in out_names
    schema_fields = {
        f["name"]
        for o in event["outputs"]
        if o["name"] == "gold_current_prices"
        for f in o["facets"]["schema"]["fields"]
    }
    assert "price_id" in schema_fields
    # Non-int count values are excluded from the facet; int ones kept.
    assert event["run"]["facets"]["processing_counts"]["prices"] == 3
    assert "skip" not in event["run"]["facets"]["processing_counts"]
    # The event landed on disk.
    assert len(list(tmp_path.glob("normalize_*.json"))) == 1


def test_openlineage_post_failure_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(lineage, "LINEAGE_DIR", tmp_path)
    monkeypatch.setenv("OPENLINEAGE_URL", "http://127.0.0.1:1")  # nothing listens
    con = _fresh()
    event, delivery = lineage.emit_lineage(con, "normalize")
    assert event["eventType"] == "COMPLETE"
    assert delivery.startswith("post failed") or delivery.startswith("HTTP")
