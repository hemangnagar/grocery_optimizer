"""Single-store verdict engine (build step 2, v2 pivot).

Per CLAUDE.md: basket-splitting is behaviorally weak, so the primary output is
ONE store recommendation for the whole list; the multi-store split is demoted
to a footnote. Reads only gold views (plus ``optimize_basket`` for the split
numbers), so every number here is queryable from gold.

Coverage rule: a store can only WIN if it covers every basket item. Stores that
don't are still shown, flagged with which items they're missing, never silently
dropped.

Substitution modes: "exact brands" vs "flexible (store brands OK)", driven by
each price row's ``match_confidence`` (RapidFuzz score / 1.0 for exact-seed-
manual matches) rather than a brand field — the schema doesn't track a
requested brand yet (that's a step-3/4 concern once real user lists exist).
Flexible reuses gold's existing trust threshold; exact requires a much tighter
match.
"""

from __future__ import annotations

import duckdb

from .optimize import optimize_basket

FLEXIBLE_MIN_CONFIDENCE = 0.85  # same threshold gold_current_prices already trusts
EXACT_MIN_CONFIDENCE = 0.97  # near-exact name match only; excludes fuzzy substitutes

# "Probably not worth it" heuristic for the split-savings footnote: a
# deterministic, tunable rule (not LLM) - flag as worth it only if the split
# saves a meaningful flat amount OR a meaningful share of the winning total.
WORTH_IT_MIN_SAVINGS = 10.0
WORTH_IT_MIN_PCT = 0.08


def _store_totals(
    con: duckdb.DuckDBPyConnection, basket_id: int, min_confidence: float
) -> list[dict]:
    """Per-store coverage + total at a given match-confidence threshold.

    Sorted so a full-coverage store always sorts before a partial one, cheapest
    first within each group - stores[0] is the winner iff it's full coverage.
    """
    all_labels = [
        r[0]
        for r in con.execute(
            "SELECT label FROM basket_items WHERE basket_id = ? ORDER BY label",
            [basket_id],
        ).fetchall()
    ]
    n_items = len(all_labels)

    rows = con.execute(
        """
        WITH offers AS (
            SELECT bi.label, g.source, bi.quantity, min(g.price) AS price
            FROM basket_items bi
            JOIN gold_current_prices g ON g.canonical_id = bi.canonical_id
            WHERE bi.basket_id = ? AND g.match_confidence >= ?
            GROUP BY bi.label, g.source, bi.quantity
        )
        SELECT source, list(DISTINCT label) AS covered_labels,
               round(sum(price * quantity), 2) AS store_total
        FROM offers
        GROUP BY source
        """,
        [basket_id, min_confidence],
    ).fetchall()

    results = []
    for source, covered_labels, store_total in rows:
        covered = set(covered_labels)
        missing = [label for label in all_labels if label not in covered]
        results.append(
            {
                "source": source,
                "items_covered": len(covered),
                "n_items": n_items,
                "store_total": float(store_total),
                "missing_labels": missing,
                "full_coverage": not missing,
            }
        )
    results.sort(key=lambda s: (not s["full_coverage"], s["store_total"]))
    return results


def _mode_verdict(
    con: duckdb.DuckDBPyConnection, basket_id: int, min_confidence: float
) -> dict:
    stores = _store_totals(con, basket_id, min_confidence)
    winner = stores[0] if stores and stores[0]["full_coverage"] else None
    return {"min_confidence": min_confidence, "winner": winner, "stores": stores}


def build_verdict(con: duckdb.DuckDBPyConnection, basket_id: int) -> dict:
    """The step-2 verdict: single-store winner (two substitution modes) plus
    the multi-store split demoted to a savings footnote."""
    opt = optimize_basket(con, basket_id)
    split_total = float(opt["split_total"])
    flexible = _mode_verdict(con, basket_id, FLEXIBLE_MIN_CONFIDENCE)
    exact = _mode_verdict(con, basket_id, EXACT_MIN_CONFIDENCE)

    winner = flexible["winner"]
    footnote = None
    if winner is not None:
        split_delta = round(winner["store_total"] - split_total, 2)
        if split_delta > 0:
            pct = split_delta / winner["store_total"] if winner["store_total"] else 0.0
            footnote = {
                "split_total": split_total,
                "stores_used": opt["stores_used"],
                "savings": split_delta,
                "worth_it": split_delta >= WORTH_IT_MIN_SAVINGS or pct >= WORTH_IT_MIN_PCT,
            }
    elif opt["n_items"] > 0:
        # No single store covers the whole list at any confidence - the split
        # isn't optional, so there's no "worth it" delta to weigh.
        footnote = {
            "split_total": split_total,
            "stores_used": opt["stores_used"],
            "savings": None,
            "worth_it": None,
        }

    return {
        "basket_id": basket_id,
        "n_items": opt["n_items"],
        "flexible": flexible,
        "exact": exact,
        "split_footnote": footnote,
    }
