"""Synthetic family-of-four weekly basket generator.

Per the spec's synthetic-first validation: build a realistic weekly basket
(produce, proteins, dairy, kids' snacks, staples) and ground each line in a real
canonical product that actually has a price in gold, so the optimizer runs
against real scraped prices. Deterministic (fixed list, no randomness).
"""

from __future__ import annotations

import duckdb

# (category, label, match keyword, weekly quantity) for a family of four.
SHOPPING_LIST = [
    ("produce", "bananas", "banana", 2),
    ("produce", "apples", "apple", 1),
    ("produce", "potatoes", "russet potato", 1),
    ("produce", "onions", "yellow onion", 1),
    ("produce", "carrots", "carrot", 1),
    ("produce", "tomatoes", "tomato", 1),
    ("produce", "lettuce", "lettuce", 1),
    ("produce", "broccoli", "broccoli", 1),
    ("dairy", "milk", "milk", 2),
    ("dairy", "eggs", "egg", 1),
    ("dairy", "butter", "butter", 1),
    ("dairy", "yogurt", "yogurt", 2),
    ("dairy", "cheese", "cheddar", 1),
    ("protein", "chicken", "chicken breast", 2),
    ("protein", "ground beef", "ground beef", 1),
    ("protein", "salmon", "salmon", 1),
    ("protein", "bacon", "bacon", 1),
    ("staples", "rice", "rice", 1),
    ("staples", "pasta", "pasta", 2),
    ("staples", "bread", "bread", 2),
    ("staples", "cereal", "cereal", 2),
    ("staples", "peanut butter", "peanut butter", 1),
    ("staples", "olive oil", "olive oil", 1),
    ("snacks", "crackers", "cracker", 1),
    ("snacks", "chips", "chips", 1),
    ("snacks", "cookies", "cookie", 1),
]


# Shopping-list category -> coarse taxonomy category (silver.taxonomy). The
# hard guard: a list item may only match products in its own coarse category
# ("chicken" can never pick "chicken crackers" — that's a snack).
_LIST_TO_COARSE = {
    "produce": "produce",
    "dairy": "dairy",
    "protein": "protein",
    "staples": "pantry",
    "snacks": "snack",
}


# Product types that are lexical false-friends of staples (e.g. "Rice Pudding"
# for rice, "Marshmallow Eggs" for eggs). Deterministic guard; precise semantic
# item selection is the job of the step-8 LLM narrator/adjudicator.
_FALSE_FRIENDS = (
    "pudding|marshmallow|candle|sunscreen|scented|shampoo|detergent|lotion"
    "|candy|candies|juice|soda|drink|\\bmix\\b|cake|frosting|creamer|cocktail|flavored"
)


def _match_canonical(
    con, keyword: str, exclude: set[int], coarse: str | None = None
) -> tuple[int, str] | None:
    """Best gold canonical for a keyword. Uses whole-word matching (so "apple"
    does not match "Pineapple"), prefers names that START with the term, then
    multi-source, then a short (generic) name, then cheapest. Blocks obvious
    false-friend product types, and (hard rule) never crosses coarse
    categories when both the list item's and the product's category are known."""
    kw = keyword.lower()
    word_re = r"\b" + kw.replace(" ", r"\s+") + r"s?\b"  # whole word, allow plural
    starts_re = "^" + kw
    rows = con.execute(
        """
        SELECT canonical_id, canonical_name,
               count(DISTINCT source) AS ns, min(price) AS mp,
               CASE WHEN regexp_matches(lower(canonical_name), ?) THEN 1 ELSE 0 END AS starts
        FROM gold_current_prices
        WHERE regexp_matches(lower(canonical_name), ?)
          AND NOT regexp_matches(lower(canonical_name), ?)
          AND (? IS NULL OR coarse_category IS NULL OR coarse_category = ?)
        GROUP BY canonical_id, canonical_name
        ORDER BY starts DESC, ns DESC, length(canonical_name) ASC, mp ASC
        """,
        [starts_re, word_re, _FALSE_FRIENDS, coarse, coarse],
    ).fetchall()
    for canonical_id, name, _ns, _mp, _starts in rows:
        if canonical_id not in exclude:
            return canonical_id, name
    return None


def generate_synthetic_basket(
    con: duckdb.DuckDBPyConnection,
    *,
    name: str = "Synthetic family-of-four week",
    household_size: int = 4,
) -> dict:
    """Create (or refresh) the synthetic basket. Returns basket_id + match info."""
    existing = con.execute(
        "SELECT basket_id FROM baskets WHERE name = ?", [name]
    ).fetchone()
    if existing:
        basket_id = existing[0]
        con.execute("DELETE FROM basket_items WHERE basket_id = ?", [basket_id])
    else:
        basket_id = con.execute(
            "INSERT INTO baskets (name, household_size) VALUES (?, ?) RETURNING basket_id",
            [name, household_size],
        ).fetchone()[0]

    chosen: set[int] = set()
    matched, missing = [], []
    for category, label, keyword, qty in SHOPPING_LIST:
        hit = _match_canonical(con, keyword, chosen, _LIST_TO_COARSE.get(category))
        if hit is None:
            missing.append(label)
            continue
        canonical_id, canonical_name = hit
        chosen.add(canonical_id)
        con.execute(
            """
            INSERT INTO basket_items (basket_id, canonical_id, category, label, quantity)
            VALUES (?, ?, ?, ?, ?)
            """,
            [basket_id, canonical_id, category, label, qty],
        )
        matched.append((label, canonical_name))

    return {
        "basket_id": basket_id,
        "matched": matched,
        "missing": missing,
        "requested": len(SHOPPING_LIST),
    }
