"""Entry point: seed a deterministic synthetic demo dataset.

Builds the small seeded demo dataset the spec's showcase phase calls for: four
stores, the synthetic family-of-four basket, and realistic (invented) prices so
the verdict engine, API, and PWA all run end-to-end with zero credentials and
zero harvested price history. Everything is inserted through silver, so gold
views serve it exactly like real data. Idempotent — re-running replaces the
demo rows. Usage::

    uv run grocery-seed-demo

The price design is intentional, not random:

- Harris Teeter covers the whole list and is cheapest overall -> flexible winner.
- Two of its matches are store-brand substitutes (confidence 0.90), so in
  exact-brands mode (>= 0.97) it loses coverage and Whole Foods — pricier but
  exact — takes the verdict. The two modes genuinely diverge.
- Aldi is cheapest per item but misses several items -> coverage flag, never wins.
- Trader Joe's sits mid-pack with a few gaps.
- The perfect 4-store split lands ~$24 under the winner (Aldi really is that
  much cheaper), so the split footnote shows a real, non-trivial delta.
"""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb

from ..db import get_connection, init_db
from ..silver.basket import SHOPPING_LIST
from ..silver.normalize import _ad_week

DEMO_BASKET_NAME = "Demo family-of-four week (synthetic)"
DEMO_METHOD = "demo_seed"

_STORES = {
    "kroger": ("HT-DEMO-1", "Harris Teeter Vienna (demo)"),
    "wholefoods": ("WFM-DEMO-1", "Whole Foods Vienna (demo)"),
    "traderjoes": ("TJ-DEMO-1", "Trader Joe's Vienna (demo)"),
    "aldi_kcl": ("ALDI-DEMO-1", "Aldi Vienna (demo)"),
}

# label -> (canonical name, coarse_category, size text,
#           {source: (price, confidence)}) — price None = store doesn't carry it.
# Labels must match silver.basket.SHOPPING_LIST so quantities line up.
_CATALOG: dict[str, tuple[str, str, str, dict]] = {
    "bananas":       ("Bananas", "produce", "per lb",
                      {"kroger": (0.59, 1.0), "wholefoods": (0.69, 0.99), "traderjoes": (0.23, 0.97), "aldi_kcl": (0.44, 0.9)}),
    "apples":        ("Gala Apples", "produce", "3 lb bag",
                      {"kroger": (4.49, 1.0), "wholefoods": (5.49, 0.99), "traderjoes": (4.29, 0.97), "aldi_kcl": (3.29, 0.9)}),
    "potatoes":      ("Russet Potatoes", "produce", "5 lb bag",
                      {"kroger": (3.99, 1.0), "wholefoods": (5.99, 0.99), "traderjoes": (4.49, 0.97), "aldi_kcl": (2.85, 0.9)}),
    "onions":        ("Yellow Onions", "produce", "3 lb bag",
                      {"kroger": (2.99, 1.0), "wholefoods": (3.99, 0.99), "traderjoes": (2.79, 0.97), "aldi_kcl": (2.15, 0.9)}),
    "carrots":       ("Carrots", "produce", "2 lb bag",
                      {"kroger": (1.99, 1.0), "wholefoods": (2.49, 0.99), "traderjoes": (1.79, 0.97), "aldi_kcl": (1.45, 0.9)}),
    "tomatoes":      ("Roma Tomatoes", "produce", "per lb",
                      {"kroger": (1.79, 1.0), "wholefoods": (2.49, 0.99), "traderjoes": (1.99, 0.97), "aldi_kcl": (1.25, 0.9)}),
    "lettuce":       ("Romaine Lettuce Hearts", "produce", "3 ct",
                      {"kroger": (3.49, 1.0), "wholefoods": (4.29, 0.99), "traderjoes": (3.29, 0.97), "aldi_kcl": (2.89, 0.9)}),
    "broccoli":      ("Broccoli Crowns", "produce", "per lb",
                      {"kroger": (2.29, 1.0), "wholefoods": (2.99, 0.99), "traderjoes": (2.49, 0.97), "aldi_kcl": None}),
    "milk":          ("Whole Milk", "dairy", "1 gal",
                      {"kroger": (3.79, 1.0), "wholefoods": (4.79, 0.99), "traderjoes": (3.99, 0.97), "aldi_kcl": (3.15, 0.9)}),
    "eggs":          ("Large Eggs", "dairy", "12 ct",
                      {"kroger": (3.29, 1.0), "wholefoods": (4.49, 0.99), "traderjoes": (2.99, 0.97), "aldi_kcl": (2.65, 0.9)}),
    "butter":        ("Salted Butter", "dairy", "16 oz",
                      {"kroger": (4.49, 1.0), "wholefoods": (5.49, 0.99), "traderjoes": (3.99, 0.97), "aldi_kcl": (3.75, 0.9)}),
    "yogurt":        ("Greek Yogurt", "dairy", "32 oz",
                      {"kroger": (4.99, 0.90), "wholefoods": (5.99, 0.99), "traderjoes": (5.49, 0.97), "aldi_kcl": (4.29, 0.9)}),
    "cheese":        ("Sharp Cheddar", "dairy", "8 oz",
                      {"kroger": (2.99, 1.0), "wholefoods": (4.99, 0.99), "traderjoes": (3.49, 0.97), "aldi_kcl": (2.49, 0.9)}),
    "chicken":       ("Boneless Chicken Breast", "protein", "per lb",
                      {"kroger": (3.49, 1.0), "wholefoods": (6.99, 0.99), "traderjoes": (5.99, 0.97), "aldi_kcl": (2.89, 0.9)}),
    "ground beef":   ("Ground Beef 80/20", "protein", "per lb",
                      {"kroger": (4.99, 1.0), "wholefoods": (6.99, 0.99), "traderjoes": (5.49, 0.97), "aldi_kcl": (4.15, 0.9)}),
    "salmon":        ("Atlantic Salmon Fillet", "protein", "per lb",
                      {"kroger": (9.99, 1.0), "wholefoods": (12.99, 0.99), "traderjoes": (11.49, 0.97), "aldi_kcl": None}),
    "bacon":         ("Hickory Smoked Bacon", "protein", "16 oz",
                      {"kroger": (5.99, 1.0), "wholefoods": (7.99, 0.99), "traderjoes": (5.49, 0.97), "aldi_kcl": (4.45, 0.9)}),
    "rice":          ("Jasmine Rice", "pantry", "5 lb",
                      {"kroger": (6.49, 1.0), "wholefoods": (8.99, 0.99), "traderjoes": (7.99, 0.97), "aldi_kcl": (5.49, 0.9)}),
    "pasta":         ("Penne Pasta", "pantry", "16 oz",
                      {"kroger": (1.29, 1.0), "wholefoods": (1.99, 0.99), "traderjoes": (1.49, 0.97), "aldi_kcl": (0.95, 0.9)}),
    "bread":         ("Whole Wheat Bread", "pantry", "20 oz loaf",
                      {"kroger": (2.79, 1.0), "wholefoods": (3.99, 0.99), "traderjoes": (2.99, 0.97), "aldi_kcl": (1.95, 0.9)}),
    "cereal":        ("Honey Oat Cereal", "pantry", "18 oz",
                      {"kroger": (3.49, 0.90), "wholefoods": (4.99, 0.99), "traderjoes": (3.99, 0.97), "aldi_kcl": (2.79, 0.9)}),
    "peanut butter": ("Creamy Peanut Butter", "pantry", "16 oz",
                      {"kroger": (2.99, 1.0), "wholefoods": (4.49, 0.99), "traderjoes": (2.49, 0.97), "aldi_kcl": (2.15, 0.9)}),
    "olive oil":     ("Extra Virgin Olive Oil", "pantry", "16.9 fl oz",
                      {"kroger": (8.99, 1.0), "wholefoods": (11.99, 0.99), "traderjoes": (7.99, 0.97), "aldi_kcl": (6.95, 0.9)}),
    "crackers":      ("Cheese Crackers", "snack", "12.4 oz",
                      {"kroger": (3.29, 1.0), "wholefoods": (4.49, 0.99), "traderjoes": None, "aldi_kcl": (2.35, 0.9)}),
    "chips":         ("Tortilla Chips", "snack", "13 oz",
                      {"kroger": (3.79, 1.0), "wholefoods": (4.29, 0.99), "traderjoes": (2.99, 0.97), "aldi_kcl": (2.25, 0.9)}),
    "cookies":       ("Chocolate Chip Cookies", "snack", "13 oz",
                      {"kroger": (3.99, 1.0), "wholefoods": (5.49, 0.99), "traderjoes": (4.49, 0.97), "aldi_kcl": None}),
}

# A few weekly-ad promos for UI flavor: (label, source, regular_price).
_PROMOS = {
    ("chicken", "kroger"): 5.99,
    ("cheese", "kroger"): 4.29,
    ("apples", "aldi_kcl"): 4.19,
}


def clear_demo(con: duckdb.DuckDBPyConnection) -> None:
    """Remove previously seeded demo rows (safe: demo rows only)."""
    con.execute(
        "DELETE FROM prices WHERE source_product_id IN "
        "(SELECT source_product_id FROM source_products WHERE match_method = ?)",
        [DEMO_METHOD],
    )
    con.execute(
        "DELETE FROM basket_items WHERE basket_id IN "
        "(SELECT basket_id FROM baskets WHERE name = ?)",
        [DEMO_BASKET_NAME],
    )
    con.execute("DELETE FROM baskets WHERE name = ?", [DEMO_BASKET_NAME])
    con.execute(
        "DELETE FROM canonical_products WHERE canonical_id IN "
        "(SELECT canonical_id FROM source_products WHERE match_method = ?)",
        [DEMO_METHOD],
    )
    con.execute("DELETE FROM source_products WHERE match_method = ?", [DEMO_METHOD])


def seed_demo(con: duckdb.DuckDBPyConnection) -> dict:
    """Insert the demo dataset through silver. Returns summary counts."""
    clear_demo(con)
    now = datetime.now(timezone.utc)
    week = _ad_week(now)

    # traderjoes joined after the schema seed, so its sources row may be absent.
    con.execute(
        "INSERT INTO sources (source, banner, chain_parent, access_method, confidence_tier) "
        "VALUES ('traderjoes', 'Trader Joe''s', 'Trader Joe''s', 'scrape', 'high') "
        "ON CONFLICT (source) DO NOTHING"
    )

    for source, (store_id, name) in _STORES.items():
        con.execute(
            "INSERT INTO stores (source, store_id, name, region) VALUES (?, ?, ?, 'demo') "
            "ON CONFLICT (source, store_id) DO NOTHING",
            [source, store_id, name],
        )

    basket_id = con.execute(
        "INSERT INTO baskets (name, household_size, is_synthetic, notes) "
        "VALUES (?, 4, true, 'seeded demo dataset') RETURNING basket_id",
        [DEMO_BASKET_NAME],
    ).fetchone()[0]

    n_prices = 0
    for category, label, _keyword, qty in SHOPPING_LIST:
        cname, coarse, size_text, offers = _CATALOG[label]
        canonical_id = con.execute(
            "INSERT INTO canonical_products (name, category, coarse_category) "
            "VALUES (?, ?, ?) RETURNING canonical_id",
            [cname, category, coarse],
        ).fetchone()[0]
        con.execute(
            "INSERT INTO basket_items (basket_id, canonical_id, category, label, quantity) "
            "VALUES (?, ?, ?, ?, ?)",
            [basket_id, canonical_id, category, label, qty],
        )
        for source, offer in offers.items():
            if offer is None:
                continue
            price, confidence = offer
            spid = con.execute(
                """
                INSERT INTO source_products (
                    source, source_sku, raw_name, raw_size_text, coarse_category,
                    canonical_id, match_confidence, match_method, resolved_at,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING source_product_id
                """,
                [source, f"demo-{source}-{label}", cname, size_text, coarse,
                 canonical_id, confidence, DEMO_METHOD, now, now, now],
            ).fetchone()[0]
            regular = _PROMOS.get((label, source))
            con.execute(
                """
                INSERT INTO prices (
                    source_product_id, source, store_id, region, observed_at,
                    ad_week, price, regular_price, is_promo, discount_pct, confidence
                ) VALUES (?, ?, ?, 'demo', ?, ?, ?, ?, ?, ?, 0.99)
                """,
                [spid, source, _STORES[source][0], now, week, price, regular,
                 regular is not None,
                 round((regular - price) / regular * 100) if regular else None],
            )
            n_prices += 1

    return {"basket_id": basket_id, "items": len(SHOPPING_LIST), "prices": n_prices}


def main() -> None:
    con = get_connection()
    try:
        init_db(con)
        result = seed_demo(con)
        print(
            f"Seeded demo basket {result['basket_id']}: {result['items']} items, "
            f"{result['prices']} prices across {len(_STORES)} stores."
        )
        print("Run `uv run grocery-serve` and open the PWA, or `uv run grocery-optimize`.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
