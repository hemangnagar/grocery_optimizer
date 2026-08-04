"""Tests for v2 step 4: verdict cache + LLM list-term adjudicator + review queue."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest

from grocery_optimizer.db import init_db
from grocery_optimizer.scripts.adjudicate_list import build_pairs
from grocery_optimizer.silver.basket import _match_canonical, generate_synthetic_basket
from grocery_optimizer.silver.verdict_cache import (
    adjudicate_pairs,
    confident_matches,
    enqueue_review,
    get_verdict,
    normalize_term,
    review_verdict,
    store_verdict,
)


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    init_db(c)
    yield c
    c.close()


def _add_priced(con, name, coarse, price, source="kroger"):
    """Canonical + trusted source product + price, so it appears in gold."""
    now = datetime.now(timezone.utc)
    cid = con.execute(
        "INSERT INTO canonical_products (name, coarse_category) VALUES (?, ?) "
        "RETURNING canonical_id",
        [name, coarse],
    ).fetchone()[0]
    spid = con.execute(
        """
        INSERT INTO source_products (
            source, source_sku, raw_name, coarse_category, canonical_id,
            match_confidence, match_method, resolved_at, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, 1.0, 'manual', ?, ?, ?)
        RETURNING source_product_id
        """,
        [source, f"sku-{source}-{name}", name, coarse, cid, now, now, now],
    ).fetchone()[0]
    con.execute(
        "INSERT INTO prices (source_product_id, source, observed_at, price, confidence) "
        "VALUES (?, ?, ?, ?, 0.99)",
        [spid, source, now, price],
    )
    return cid


def _counting_decider(verdicts: dict):
    """decide() stub keyed on candidate name; counts LLM calls."""
    calls = []

    def decide(term, candidate):
        calls.append((term["keyword"], candidate["name"]))
        return verdicts[candidate["name"]]

    decide.calls = calls
    return decide


# ---- cache-before-LLM ------------------------------------------------------

def test_each_pair_costs_one_llm_call_ever(con):
    bites = _add_priced(con, "Chicken Breast Bites", "protein", 6.99)
    breast = _add_priced(con, "Boneless Chicken Breast", "protein", 3.49)
    decider = _counting_decider({
        "Chicken Breast Bites": {"verdict": "no_match", "confidence": 0.95, "reason": "snack-form"},
        "Boneless Chicken Breast": {"verdict": "match", "confidence": 0.97, "reason": "base item"},
    })
    pairs = [
        {"keyword": "chicken breast", "category": "protein",
         "candidate": {"canonical_id": bites, "name": "Chicken Breast Bites", "coarse_category": "protein"}},
        {"keyword": "chicken breast", "category": "protein",
         "candidate": {"canonical_id": breast, "name": "Boneless Chicken Breast", "coarse_category": "protein"}},
    ]
    first = adjudicate_pairs(con, decider, pairs)
    assert first["llm_calls"] == 2 and first["cache_hits"] == 0

    second = adjudicate_pairs(con, decider, pairs)
    assert second["llm_calls"] == 0 and second["cache_hits"] == 2
    assert len(decider.calls) == 2  # one call per pair, ever


def test_selection_reads_only_the_cache(con):
    # The keyword heuristic prefers "Chicken Breast Bites" (starts with the
    # term); the cached verdicts must overrule it deterministically.
    bites = _add_priced(con, "Chicken Breast Bites", "protein", 2.99)
    breast = _add_priced(con, "Boneless Chicken Breast", "protein", 3.49)

    before = _match_canonical(con, "chicken breast", set(), "protein")
    assert before == (bites, "Chicken Breast Bites")  # the known failure mode

    norm = normalize_term("chicken breast")
    store_verdict(con, norm, bites, "no_match", 0.95, "snack-form product")
    store_verdict(con, norm, breast, "match", 0.97, "base item")

    after = _match_canonical(con, "chicken breast", set(), "protein")
    assert after == (breast, "Boneless Chicken Breast")

    # generate_synthetic_basket flows through the same selection
    result = generate_synthetic_basket(con)
    picked = dict(result["matched"])
    assert picked.get("chicken") == "Boneless Chicken Breast"


def test_category_guard_outranks_cache(con):
    # Even a confident cached match is ignored across coarse categories.
    crackers = _add_priced(con, "Chicken Crackers", "snack", 3.29)
    store_verdict(con, normalize_term("chicken"), crackers, "match", 0.99, "wrong")
    assert confident_matches(con, normalize_term("chicken"), "protein") == []
    assert confident_matches(con, normalize_term("chicken"), "snack") != []


# ---- review queue ----------------------------------------------------------

def test_below_threshold_is_queued_and_not_used(con):
    tenders = _add_priced(con, "Breaded Chicken Tenders", "protein", 5.99)
    decider = _counting_decider({
        "Breaded Chicken Tenders": {"verdict": "match", "confidence": 0.6, "reason": "unsure"},
    })
    pairs = [{"keyword": "chicken", "category": "protein",
              "candidate": {"canonical_id": tenders, "name": "Breaded Chicken Tenders",
                            "coarse_category": "protein"}}]
    stats = adjudicate_pairs(con, decider, pairs)
    assert stats["queued_for_review"] == 1

    # stored but not confident -> selection ignores it
    assert get_verdict(con, normalize_term("chicken"), tenders)["confidence"] == 0.6
    assert confident_matches(con, normalize_term("chicken"), "protein") == []
    assert con.execute(
        "SELECT count(*) FROM match_review_queue WHERE status = 'open'"
    ).fetchone()[0] == 1


def test_review_approve_makes_verdict_usable(con):
    tenders = _add_priced(con, "Breaded Chicken Tenders", "protein", 5.99)
    vid = store_verdict(con, normalize_term("chicken"), tenders, "match", 0.6, "unsure")
    enqueue_review(con, vid)
    review_verdict(con, vid, approve=True)

    v = get_verdict(con, normalize_term("chicken"), tenders)
    assert v["decided_by"] == "human" and v["confidence"] == 1.0
    assert confident_matches(con, normalize_term("chicken"), "protein") == [
        (tenders, "Breaded Chicken Tenders")
    ]
    assert con.execute(
        "SELECT status FROM match_review_queue WHERE verdict_id = ?", [vid]
    ).fetchone()[0] == "approved"


def test_review_reject_flips_verdict(con):
    bites = _add_priced(con, "Chicken Breast Bites", "protein", 2.99)
    vid = store_verdict(con, normalize_term("chicken"), bites, "match", 0.7, "unsure")
    enqueue_review(con, vid)
    review_verdict(con, vid, approve=False)

    v = get_verdict(con, normalize_term("chicken"), bites)
    assert v["verdict"] == "no_match" and v["confidence"] == 1.0
    # and the heuristic fallback now excludes it too
    assert _match_canonical(con, "chicken", set(), "protein") is None


# ---- stable pair set for adjudication --------------------------------------

def test_build_pairs_is_stable_after_no_match_verdicts(con):
    bites = _add_priced(con, "Chicken Breast Bites", "protein", 2.99)
    _add_priced(con, "Boneless Chicken Breast", "protein", 3.49)
    before = {
        (p["keyword"], p["candidate"]["canonical_id"]) for p in build_pairs(con)
    }
    store_verdict(con, normalize_term("chicken breast"), bites, "no_match", 0.95, "snack")
    after = {
        (p["keyword"], p["candidate"]["canonical_id"]) for p in build_pairs(con)
    }
    # a cached no_match must NOT shrink the adjudication pair set (it's skipped
    # as a cache hit instead), or reruns would spend tokens on new candidates
    assert before == after
