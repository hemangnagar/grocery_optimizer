"""Parser self-healing agent tests: drift detection (raise vs silent-zero vs
legit-empty), the deterministic validation gate, promotion/rejection wiring,
and the ingest overlay. The LLM proposer is faked — no network."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from grocery_optimizer.db import init_db
from grocery_optimizer.silver import parser_heal
from grocery_optimizer.silver.normalize import ingest_bronze, parse_kroger_products
from grocery_optimizer.silver.parser_heal import (
    detect_drift,
    heal,
    load_promoted_parser,
    validate_patch,
)

FIXTURES = Path(__file__).parent / "fixtures"
KNOWN_GOOD = (FIXTURES / "kroger_products_sample.json").read_bytes()


def _drifted(raw: bytes = KNOWN_GOOD) -> bytes:
    """Simulate upstream drift: the top-level 'data' container was renamed."""
    payload = json.loads(raw)
    payload["productsList"] = payload.pop("data")
    return json.dumps(payload).encode()


# A correct patch: handles both the old and the drifted container name and
# otherwise mirrors parse_kroger_products exactly (the gate demands it).
GOOD_PATCH = '''
def parse(raw):
    payload = json.loads(raw)
    products = payload.get("data")
    if products is None:
        products = payload.get("productsList") or []
    records = []
    for product in products:
        items = product.get("items") or []
        item = items[0] if items else {}
        price_block = item.get("price") or {}
        regular = price_block.get("regular")
        promo = price_block.get("promo")
        is_promo = bool(promo) and promo > 0
        price = promo if is_promo else regular
        size_text = item.get("size")
        up, unit = unit_price(price, size_text)
        cats = product.get("categories") or []
        discount_pct = (
            round((regular - promo) / regular * 100)
            if is_promo and regular
            else None
        )
        records.append(
            {
                "source": "kroger",
                "source_sku": product.get("productId"),
                "raw_name": product.get("description"),
                "raw_brand": product.get("brand"),
                "raw_size_text": size_text,
                "category_hint": cats[0] if cats else None,
                "price": price,
                "regular_price": regular if is_promo else None,
                "unit_price": up,
                "unit": unit,
                "is_promo": is_promo,
                "discount_pct": discount_pct,
                "promo_text": None,
            }
        )
    return records
'''

# Wrong on known-good bronze: drops the brand field.
BAD_PATCH = GOOD_PATCH.replace('product.get("brand")', "None")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    bronze = tmp_path / "bronze"
    bronze.mkdir()
    monkeypatch.setattr(parser_heal.config, "BRONZE_DIR", bronze)
    monkeypatch.setattr(parser_heal, "PATCH_DIR", tmp_path / "patches")
    con = duckdb.connect(":memory:")
    init_db(con)
    return con, bronze


def _manifest(con, bronze: Path, name: str, content: bytes, status: str) -> int:
    (bronze / name).write_bytes(content)
    return con.execute(
        """
        INSERT INTO bronze_manifest (source, source_kind, fetched_at, raw_path,
                                     parse_status, request_params)
        VALUES ('kroger', 'api', CURRENT_TIMESTAMP, ?, ?,
                '{"endpoint": "products"}') RETURNING manifest_id
        """,
        [name, status],
    ).fetchone()[0]


# -- detection ---------------------------------------------------------------

def test_detects_silently_zero_record_drift(env):
    con, bronze = env
    good_id = _manifest(con, bronze, "good.json", KNOWN_GOOD, "parsed")
    drift_id = _manifest(con, bronze, "drift.json", _drifted(), "pending")

    found = detect_drift(con)
    assert len(found) == 1
    assert found[0]["source"] == "kroger"
    assert found[0]["drift_manifest_id"] == drift_id
    assert found[0]["known_good_manifest_id"] == good_id


def test_legit_empty_payload_is_not_drift(env):
    con, bronze = env
    _manifest(con, bronze, "good.json", KNOWN_GOOD, "parsed")
    _manifest(con, bronze, "empty.json", b'{"data": []}', "empty")
    assert detect_drift(con) == []


def test_detects_raising_drift(env):
    con, bronze = env
    _manifest(con, bronze, "good.json", KNOWN_GOOD, "parsed")
    _manifest(con, bronze, "broken.json", b"<html>not json</html>", "error")
    found = detect_drift(con)
    assert len(found) == 1


# -- the gate ----------------------------------------------------------------

def test_gate_promotes_only_exact_reproduction(env):
    ok, reason = validate_patch(GOOD_PATCH, parse_kroger_products, KNOWN_GOOD, _drifted())
    assert ok, reason

    ok, reason = validate_patch(BAD_PATCH, parse_kroger_products, KNOWN_GOOD, _drifted())
    assert not ok
    assert "diverges" in reason


def test_gate_rejects_imports_and_missing_parse():
    ok, reason = validate_patch(
        "import os\ndef parse(raw):\n    return []",
        parse_kroger_products, KNOWN_GOOD, _drifted(),
    )
    assert not ok and "import" in reason

    ok, reason = validate_patch(
        "def not_parse(raw):\n    return []",
        parse_kroger_products, KNOWN_GOOD, _drifted(),
    )
    assert not ok and "no parse" in reason


# -- heal + ingest overlay ---------------------------------------------------

def test_heal_promotes_and_ingest_uses_patch(env):
    con, bronze = env
    _manifest(con, bronze, "good.json", KNOWN_GOOD, "parsed")
    drift_id = _manifest(con, bronze, "drift.json", _drifted(), "pending")

    results = heal(con, lambda src, diff, sample: {"code": GOOD_PATCH, "notes": "renamed"})
    assert [r["status"] for r in results] == ["promoted"]

    patched = load_promoted_parser(con, "kroger")
    assert patched is not None
    assert len(patched(_drifted())) == len(parse_kroger_products(KNOWN_GOOD))

    counts = ingest_bronze(con)
    assert counts["prices"] > 0
    status = con.execute(
        "SELECT parse_status FROM bronze_manifest WHERE manifest_id = ?", [drift_id]
    ).fetchone()[0]
    assert status == "parsed"


def test_heal_rejects_bad_patch_and_skips_retry(env):
    con, bronze = env
    _manifest(con, bronze, "good.json", KNOWN_GOOD, "parsed")
    _manifest(con, bronze, "drift.json", _drifted(), "pending")

    results = heal(con, lambda src, diff, sample: {"code": BAD_PATCH, "notes": ""})
    assert [r["status"] for r in results] == ["rejected"]
    assert load_promoted_parser(con, "kroger") is None

    # Second run must not re-spend a proposal on the same drift artifact.
    results = heal(con, lambda src, diff, sample: pytest.fail("proposer re-called"))
    assert [r["status"] for r in results] == ["skipped"]


def test_zero_record_artifact_marked_empty_not_parsed(env):
    con, bronze = env
    mid = _manifest(con, bronze, "drift.json", _drifted(), "pending")
    ingest_bronze(con)
    status = con.execute(
        "SELECT parse_status FROM bronze_manifest WHERE manifest_id = ?", [mid]
    ).fetchone()[0]
    assert status == "empty"
