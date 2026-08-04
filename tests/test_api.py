"""Tests for v2 step 5: seeded demo dataset + FastAPI gold endpoints."""

from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient

from grocery_optimizer import config
from grocery_optimizer.api.app import create_app
from grocery_optimizer.db import init_db
from grocery_optimizer.scripts.seed_demo import seed_demo


@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    db_path = tmp_path / "demo.duckdb"
    con = duckdb.connect(str(db_path))
    init_db(con)
    seed_demo(con)
    con.close()
    monkeypatch.setattr(config, "DB_PATH", db_path)
    return db_path


@pytest.fixture()
def client(seeded_db):
    return TestClient(create_app())


def test_seed_is_idempotent(seeded_db):
    con = duckdb.connect(str(seeded_db))
    before = con.execute("SELECT count(*) FROM prices").fetchone()[0]
    seed_demo(con)
    after = con.execute("SELECT count(*) FROM prices").fetchone()[0]
    baskets = con.execute("SELECT count(*) FROM baskets").fetchone()[0]
    con.close()
    assert after == before
    assert baskets == 1


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_verdict_modes_diverge(client):
    body = client.get("/api/verdict").json()

    # Flexible: Harris Teeter (kroger) wins with full coverage, cheapest.
    flex_winner = body["flexible"]["winner"]
    assert flex_winner is not None
    assert flex_winner["source"] == "kroger"
    assert flex_winner["full_coverage"] is True
    assert flex_winner["items_covered"] == 26

    # Exact brands: kroger's two 0.90-confidence store-brand subs drop out, so
    # Whole Foods is the only full-coverage store — pricier but exact.
    exact_winner = body["exact"]["winner"]
    assert exact_winner is not None
    assert exact_winner["source"] == "wholefoods"
    exact_kroger = next(s for s in body["exact"]["stores"] if s["source"] == "kroger")
    assert exact_kroger["items_covered"] == 24
    assert "yogurt" in exact_kroger["missing_labels"]
    assert "cereal" in exact_kroger["missing_labels"]
    assert exact_winner["store_total"] > flex_winner["store_total"]

    # Aldi never wins: cheap but partial coverage in flexible mode.
    aldi = next(s for s in body["flexible"]["stores"] if s["source"] == "aldi_kcl")
    assert aldi["full_coverage"] is False
    assert len(aldi["missing_labels"]) == 3

    # Split footnote: real savings vs the flexible winner.
    fn = body["split_footnote"]
    assert fn is not None
    assert fn["split_total"] < flex_winner["store_total"]
    assert fn["savings"] == pytest.approx(
        round(flex_winner["store_total"] - fn["split_total"], 2)
    )

    # Metadata the UI needs.
    assert body["basket"]["is_synthetic"] is True
    assert body["banners"]["kroger"]
    assert body["ad_week"]


def test_winner_is_cheapest_full_coverage(client):
    body = client.get("/api/verdict").json()
    for mode in ("flexible", "exact"):
        stores = body[mode]["stores"]
        winner = body[mode]["winner"]
        full = [s for s in stores if s["full_coverage"]]
        assert winner == min(full, key=lambda s: s["store_total"])


def test_basket_endpoint(client):
    body = client.get("/api/basket").json()
    assert len(body["lines"]) == 26
    # every line is the cheapest offer, and traceable (price_id present)
    assert all(line["price_id"] is not None for line in body["lines"])


def test_verdict_404_when_empty(tmp_path, monkeypatch):
    db_path = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(db_path))
    init_db(con)
    con.close()
    monkeypatch.setattr(config, "DB_PATH", db_path)
    client = TestClient(create_app())
    assert client.get("/api/verdict").status_code == 404
