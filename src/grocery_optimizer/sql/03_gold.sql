-- Gold layer: query-facing views. Read-only over silver; the deterministic
-- pipeline is the sole source of truth, so every user-facing number traces back
-- to a row here (and via ids, back to silver and bronze).
--
-- Views are CREATE OR REPLACE so re-running init picks up definition changes.

-- Stores with haversine distance from home and the radius verdict. The home
-- row comes from home_location (written by init_db from config); an empty
-- home_location, or a store with no lat/lon yet, NEVER filters anything out
-- (recall over precision, same rule as the taxonomy guard).
CREATE OR REPLACE VIEW gold_stores AS
SELECT
    s.source,
    s.store_id,
    s.banner,
    s.name,
    s.address,
    s.zip,
    s.region,
    s.lat,
    s.lon,
    CASE
        WHEN s.lat IS NOT NULL AND s.lon IS NOT NULL AND h.lat IS NOT NULL THEN
            round(2 * 3958.7613 * asin(sqrt(
                sin(radians(s.lat - h.lat) / 2) ^ 2
                + cos(radians(h.lat)) * cos(radians(s.lat))
                  * sin(radians(s.lon - h.lon) / 2) ^ 2
            )), 1)
    END AS distance_miles,
    h.radius_miles
FROM stores s
LEFT JOIN home_location h ON true;

-- The latest trusted price per (source product, store). "Trusted" = the source
-- product resolved to a canonical product with match confidence at/above
-- threshold; anything weaker sits in resolution_queue and is excluded here,
-- never surfaced to users. "Current" = most recent observation, so this works
-- for both weekly-ad prices and regular (non-ad) prices. Prices at stores
-- beyond the home radius are excluded (unknown distance never excludes).
CREATE OR REPLACE VIEW gold_current_prices AS
SELECT
    cp.canonical_id,
    cp.name              AS canonical_name,
    cp.category,
    cp.coarse_category,
    cp.canonical_unit,
    p.source,
    p.store_id,
    st.name              AS store_name,
    st.region            AS store_region,
    st.distance_miles,
    p.ad_week,
    p.observed_at,
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
LEFT JOIN gold_stores st ON st.source = p.source AND st.store_id = p.store_id
WHERE sp.canonical_id IS NOT NULL
  AND sp.match_confidence >= 0.85
  AND (st.distance_miles IS NULL
       OR st.radius_miles IS NULL
       OR st.distance_miles <= st.radius_miles)
QUALIFY row_number() OVER (
    PARTITION BY p.source_product_id, p.store_id
    ORDER BY p.observed_at DESC, p.price_id DESC
) = 1;

-- Cheapest source per canonical item this week (min canonical unit price).
-- Ties broken deterministically by source then store for stable output.
CREATE OR REPLACE VIEW gold_cheapest_source_per_item AS
SELECT *
FROM gold_current_prices
QUALIFY row_number() OVER (
    PARTITION BY canonical_id
    ORDER BY unit_price ASC NULLS LAST, source ASC, store_id ASC
) = 1;

-- NOTE: basket-optimization gold view is deferred to Build Step 6 (needs the
-- synthetic basket generator + optimizer).
