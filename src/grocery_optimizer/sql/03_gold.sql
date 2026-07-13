-- Gold layer: query-facing views. Read-only over silver; the deterministic
-- pipeline is the sole source of truth, so every user-facing number traces back
-- to a row here (and via ids, back to silver and bronze).
--
-- Views are CREATE OR REPLACE so re-running init picks up definition changes.

-- Resolved, trusted prices for the current ad week (the most recent ad_week
-- present in prices). "Trusted" = the source product resolved to a canonical
-- product with match confidence at/above threshold; anything weaker sits in
-- resolution_queue and is excluded here, never surfaced to users.
CREATE OR REPLACE VIEW gold_current_prices AS
SELECT
    cp.canonical_id,
    cp.name              AS canonical_name,
    cp.category,
    cp.canonical_unit,
    p.source,
    p.store_id,
    st.name              AS store_name,
    st.region            AS store_region,
    p.ad_week,
    p.price,
    p.regular_price,
    p.unit_price,
    p.unit,
    p.is_promo,
    p.discount_pct,
    p.promo_text,
    sp.source_product_id,
    sp.match_confidence,
    p.price_id,
    p.bronze_manifest_id
FROM prices p
JOIN source_products sp ON sp.source_product_id = p.source_product_id
JOIN canonical_products cp ON cp.canonical_id = sp.canonical_id
LEFT JOIN stores st ON st.source = p.source AND st.store_id = p.store_id
WHERE sp.canonical_id IS NOT NULL
  AND sp.match_confidence >= 0.85
  AND p.ad_week = (SELECT max(ad_week) FROM prices);

-- Cheapest source per canonical item this week (min canonical unit price).
-- Ties broken deterministically by source then store for stable output.
CREATE OR REPLACE VIEW gold_cheapest_source_per_item AS
SELECT *
FROM gold_current_prices
QUALIFY row_number() OVER (
    PARTITION BY canonical_id
    ORDER BY unit_price ASC, source ASC, store_id ASC
) = 1;

-- NOTE: basket-optimization gold view is deferred to Build Step 6 (needs the
-- synthetic basket generator + optimizer).
