"""Coarse category taxonomy (v2 build step 3, layer 1 of the category guard).

Deterministic-first fix for the chicken-crackers class of bug: every source
product gets a coarse category at silver, and entity matches may NEVER cross
coarse categories. Zero tokens spent; the LLM adjudicator (v2 step 4) only ever
sees pairs that already share a category.

Classification is two-stage:

1. Source category hint (Kroger category, WFM breadcrumb, TJ hierarchy) mapped
   through keyword rules — the retailer's own taxonomy is usually right.
2. Fallback: ordered keyword rules over the product NAME. Order matters — a
   product's FORM beats its INGREDIENT ("chicken crackers" is a snack, "chicken
   broth" is pantry, "milk chocolate" is a snack), so form/prepared-product
   rules run before base-ingredient rules.

Unmappable products get None (unknown). Unknown never blocks a match — the
hard rule only applies when BOTH sides carry a category; it errs toward not
losing recall, and the adjudicator still sees those pairs.
"""

from __future__ import annotations

import re

# The complete coarse vocabulary (per CLAUDE.md). "staples" on shopping lists
# maps to "pantry" here.
COARSE_CATEGORIES = (
    "produce", "protein", "dairy", "snack", "pantry", "frozen", "household",
)

# ---- stage 1: source category hints ----------------------------------------
# First matching rule wins; hints are usually single words or short phrases.
_HINT_RULES: tuple[tuple[str, str], ...] = (
    ("frozen", "frozen"),
    ("household|cleaning|paper goods|personal care|health|beauty|baby|pet|floral", "household"),
    ("snack|candy|chips|cookies", "snack"),
    ("produce|fruit|vegetable", "produce"),
    ("meat|seafood|poultry|deli|plant.?based", "protein"),
    ("dairy|eggs|cheese", "dairy"),
    (
        "pantry|bakery|bread|baking|breakfast|cereal|canned|condiment|sauce|dips"
        "|pasta|grain|rice|international|beverage|coffee|tea|soda|juice|water"
        "|wine|beer|spirits|liquor|adult beverage",
        "pantry",
    ),
)

# ---- stage 2: product-name keywords ----------------------------------------
# FORM rules (a preparation/product form that overrides any ingredient word in
# the name) run before BASE ingredient rules. All patterns are whole-word.
_NAME_RULES: tuple[tuple[str, str], ...] = (
    # household: never food, regardless of scent/flavor words in the name
    (
        "detergent|cleaner|soap|shampoo|conditioner|lotion|sunscreen|deodorant"
        "|toothpaste|toothbrush|paper towels?|toilet paper|napkins?|tissues?"
        "|foil|trash bags?|storage bags?|sponges?|candles?|batter(?:y|ies)"
        "|diapers?|wipes|dishwash\\w*|bleach|air freshener",
        "household",
    ),
    # frozen forms
    ("frozen|ice cream|popsicles?|sherbet|sorbet|gelato", "frozen"),
    # snack forms: beat ingredient words (cheese crackers, chicken chips...)
    (
        "crackers?|chips?|cook(?:ie|ies)|cand(?:y|ies)|pretzels?|popcorn"
        "|gumm(?:y|ies)|licorice|trail mix|jerky|fruit snacks?|snack bars?"
        "|granola bars?|chocolates?|brownies?|donuts?|doughnuts?|pudding",
        "snack",
    ),
    # pantry forms: prepared/derivative products beat their base ingredient
    # (chicken broth, banana bread, egg noodles, buttermilk pancake mix)
    (
        "broths?|stocks?|soups?|bouillon|gravy|sauces?|salsa|dressings?"
        "|seasonings?|spices?|extracts?|syrups?|breads?|bagels?|buns?|rolls?"
        "|tortillas?|noodles?|pasta|cereal|oatmeal|granola|flour|sugar"
        "|mix|mixes|juices?|soda|coffee|tea|ketchup|mustard|mayonnaise|jam"
        "|jelly|honey|rice|beans?|lentils?|oil|vinegar|peanut butter|cakes?"
        "|pies?|muffins?|croissants?|crusts?|frosting|creamer",
        "pantry",
    ),
    # base ingredients
    (
        "bananas?|apples?|oranges?|grapes?|berr(?:y|ies)|strawberr(?:y|ies)"
        "|blueberr(?:y|ies)|melons?|watermelons?|peach(?:es)?|pears?|plums?"
        "|nectarines?|mango(?:e?s)?|pineapples?|avocado(?:e?s)?|lemons?|limes?"
        "|potato(?:e?s)?|onions?|carrots?|tomato(?:e?s)?|lettuce|spinach|kale"
        "|broccoli|cauliflower|celery|cucumbers?|peppers?|zucchini|squash"
        "|mushrooms?|garlic|ginger|corn|salad|greens|herbs?|cilantro|asparagus",
        "produce",
    ),
    (
        "chicken|beef|pork|turkey|lamb|salmon|tilapia|cod|tuna|shrimp|crab"
        "|fish|bacon|sausages?|ham|steaks?|drumsticks?|thighs?|breasts?"
        "|wings?|ribs?|brisket|meatballs?|tofu|tempeh|hot ?dogs?|franks?",
        "protein",
    ),
    (
        "milk|cheese|cheddar|mozzarella|parmesan|swiss|provolone|gouda|feta"
        "|yogurt|butter|eggs?|cream|half & half|half and half|cottage cheese"
        "|sour cream|cream cheese",
        "dairy",
    ),
)


def _compile(rules: tuple[tuple[str, str], ...]):
    return tuple((re.compile(r"\b(?:" + pat + r")\b"), cat) for pat, cat in rules)


_HINT_COMPILED = _compile(_HINT_RULES)
_NAME_COMPILED = _compile(_NAME_RULES)


def coarse_category(category_hint: str | None, name: str | None) -> str | None:
    """Deterministic coarse category for a product, or None if unmappable."""
    if category_hint:
        hint = category_hint.lower()
        for rx, cat in _HINT_COMPILED:
            if rx.search(hint):
                return cat
    if name:
        text = name.lower()
        for rx, cat in _NAME_COMPILED:
            if rx.search(text):
                return cat
    return None


def backfill_coarse_categories(con) -> dict:
    """Assign coarse categories to rows that predate this column.

    Idempotent: only NULL rows are touched. Canonicals classify from their own
    name; if that fails, they inherit the most common coarse category among
    their linked source products (deterministic tie-break by name).
    """
    stats = {"source_products": 0, "canonical_products": 0}
    for spid, hint, name in con.execute(
        "SELECT source_product_id, category_hint, raw_name FROM source_products "
        "WHERE coarse_category IS NULL"
    ).fetchall():
        cat = coarse_category(hint, name)
        if cat:
            con.execute(
                "UPDATE source_products SET coarse_category = ? WHERE source_product_id = ?",
                [cat, spid],
            )
            stats["source_products"] += 1

    for cid, name in con.execute(
        "SELECT canonical_id, name FROM canonical_products WHERE coarse_category IS NULL"
    ).fetchall():
        cat = coarse_category(None, name)
        if not cat:
            row = con.execute(
                """
                SELECT coarse_category FROM source_products
                WHERE canonical_id = ? AND coarse_category IS NOT NULL
                GROUP BY coarse_category
                ORDER BY count(*) DESC, coarse_category
                LIMIT 1
                """,
                [cid],
            ).fetchone()
            cat = row[0] if row else None
        if cat:
            con.execute(
                "UPDATE canonical_products SET coarse_category = ? WHERE canonical_id = ?",
                [cat, cid],
            )
            stats["canonical_products"] += 1
    return stats


def enforce_category_guard(con) -> int:
    """Unlink rapidfuzz auto-links that cross coarse categories (both known).

    The hard rule applied retroactively: cross-category fuzzy links are wrong
    by definition, so they are unlinked (dropping them out of gold) and left
    for re-resolution, which will re-match them within their own category or
    create a new canonical. LLM/manual links are trusted and left alone.
    Returns the number of links severed.
    """
    rows = con.execute(
        """
        SELECT sp.source_product_id
        FROM source_products sp
        JOIN canonical_products cp ON cp.canonical_id = sp.canonical_id
        WHERE sp.match_method = 'rapidfuzz'
          AND sp.coarse_category IS NOT NULL
          AND cp.coarse_category IS NOT NULL
          AND sp.coarse_category <> cp.coarse_category
        """
    ).fetchall()
    for (spid,) in rows:
        con.execute(
            "UPDATE source_products SET canonical_id = NULL, match_confidence = NULL, "
            "match_method = NULL, resolved_at = NULL WHERE source_product_id = ?",
            [spid],
        )
    return len(rows)
