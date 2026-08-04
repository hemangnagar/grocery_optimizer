-- Basket layer: synthetic (later real) weekly baskets + gold optimization views.
--
-- A basket is a set of canonical items with quantities. The gold views below
-- turn a basket into the cheapest store split and per-store single-shop totals,
-- reading only from gold_current_prices so every number traces back to a price
-- row (and via ids, to silver and bronze).

CREATE SEQUENCE IF NOT EXISTS seq_basket_id START 1;

CREATE TABLE IF NOT EXISTS baskets (
    basket_id      BIGINT PRIMARY KEY DEFAULT nextval('seq_basket_id'),
    name           VARCHAR NOT NULL,
    household_size INTEGER,
    is_synthetic   BOOLEAN DEFAULT true,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes          VARCHAR
);

CREATE TABLE IF NOT EXISTS basket_items (
    basket_id    BIGINT NOT NULL,
    canonical_id BIGINT NOT NULL,
    category     VARCHAR,      -- basket category (produce/dairy/protein/...)
    label        VARCHAR,      -- the shopping-list term this item satisfies
    quantity     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (basket_id, canonical_id)
);

-- Cheapest source per basket item (the optimal split): one row per item, at the
-- store with the lowest package price. line_cost = price * quantity.
CREATE OR REPLACE VIEW gold_basket_item_cost AS
SELECT *
FROM (
    SELECT
        bi.basket_id,
        bi.category,
        bi.label,
        bi.quantity,
        g.canonical_id,
        g.canonical_name,
        g.source,
        g.store_id,
        g.price,
        g.unit_price,
        g.unit,
        g.price * bi.quantity AS line_cost,
        g.price_id
    FROM basket_items bi
    JOIN gold_current_prices g ON g.canonical_id = bi.canonical_id
)
QUALIFY row_number() OVER (
    PARTITION BY basket_id, canonical_id
    ORDER BY price ASC NULLS LAST, source ASC
) = 1;

-- Weekly narrations (agentic layer #3): LLM-written prose over a deterministic
-- fact pack, stored only after the audit gate verifies every number appears in
-- gold. The API serves ONLY stored rows (narrate once, serve cached — zero
-- tokens per view, deterministic replay), with citations appended by the
-- pipeline, never by the model.
CREATE SEQUENCE IF NOT EXISTS seq_narration_id START 1;
CREATE TABLE IF NOT EXISTS narrations (
    narration_id BIGINT PRIMARY KEY DEFAULT nextval('seq_narration_id'),
    basket_id    BIGINT NOT NULL,
    ad_week      DATE,
    narration    VARCHAR NOT NULL,
    citations    VARCHAR,              -- gold row IDs, appended deterministically
    facts        VARCHAR,              -- the JSON fact pack the model saw
    model        VARCHAR,
    audit_ok     BOOLEAN NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Single-store option: for each source, how many basket items it carries and the
-- total to buy those items there (cheapest offer per item within that source).
CREATE OR REPLACE VIEW gold_basket_store_totals AS
WITH offers AS (
    SELECT
        bi.basket_id,
        bi.quantity,
        g.canonical_id,
        g.source,
        min(g.price) AS price
    FROM basket_items bi
    JOIN gold_current_prices g ON g.canonical_id = bi.canonical_id
    GROUP BY bi.basket_id, bi.quantity, g.canonical_id, g.source
)
SELECT
    basket_id,
    source,
    count(*)                 AS items_covered,
    round(sum(price * quantity), 2) AS store_total
FROM offers
GROUP BY basket_id, source;
