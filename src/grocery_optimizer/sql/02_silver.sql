-- Silver layer: parsed + normalized records.
--
-- Entity resolution maps raw source SKUs to canonical products; prices carry
-- canonical unit prices ($/lb, $/oz, $/count) and per-source confidence.
-- Foreign keys are kept as plain columns (DuckDB FK enforcement is limited);
-- referential integrity is enforced by the normalization step, not the engine.

CREATE SEQUENCE IF NOT EXISTS seq_canonical_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_source_product_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_price_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_queue_id START 1;

-- Source dimension (seeded below from the spec's feasibility table).
CREATE TABLE IF NOT EXISTS sources (
    source          VARCHAR PRIMARY KEY,   -- kroger|wholefoods|lidl|aldi_kcl|giant|safeway
    banner          VARCHAR,               -- consumer-facing banner (e.g. Harris Teeter)
    chain_parent    VARCHAR,               -- parent company (e.g. Kroger, Ahold, Albertsons)
    access_method   VARCHAR,               -- api|scrape|aggregator
    confidence_tier VARCHAR                 -- gold|high|medium
);

INSERT INTO sources (source, banner, chain_parent, access_method, confidence_tier)
VALUES
    ('kroger',     'Harris Teeter / Kroger', 'Kroger',      'api',        'gold'),
    ('wholefoods', 'Whole Foods Market',     'Amazon',      'scrape',     'high'),
    ('lidl',       'Lidl',                   'Lidl',        'scrape',     'high'),
    ('aldi_kcl',   'Aldi',                   'Aldi',        'aggregator', 'medium'),
    ('giant',      'Giant',                  'Ahold',       'aggregator', 'medium'),
    ('safeway',    'Safeway',                'Albertsons',  'aggregator', 'medium')
ON CONFLICT (source) DO NOTHING;

-- Store dimension: a physical location within a source/banner.
CREATE TABLE IF NOT EXISTS stores (
    source    VARCHAR NOT NULL,
    store_id  VARCHAR NOT NULL,
    banner    VARCHAR,
    name      VARCHAR,
    address   VARCHAR,
    zip       VARCHAR,
    region    VARCHAR,
    lat       DOUBLE,
    lon       DOUBLE,
    PRIMARY KEY (source, store_id)
);

-- Canonical products: the normalized identity every price resolves to.
CREATE TABLE IF NOT EXISTS canonical_products (
    canonical_id    BIGINT PRIMARY KEY DEFAULT nextval('seq_canonical_id'),
    name            VARCHAR NOT NULL,
    category        VARCHAR,              -- raw source category hint, free-form
    coarse_category VARCHAR,              -- produce|protein|dairy|snack|pantry|frozen|household (silver.taxonomy)
    brand          VARCHAR,               -- nullable (produce is brand-agnostic)
    canonical_unit VARCHAR,               -- lb|oz|fl_oz|count|each
    size_value     DOUBLE,                -- packaged size in canonical_unit
    notes          VARCHAR,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Source products: raw per-source listings and their resolution to canonical.
CREATE TABLE IF NOT EXISTS source_products (
    source_product_id  BIGINT PRIMARY KEY DEFAULT nextval('seq_source_product_id'),
    source             VARCHAR NOT NULL,
    source_sku         VARCHAR,           -- vendor product id (Kroger UPC, Aldi id, ...)
    raw_name           VARCHAR NOT NULL,
    raw_brand          VARCHAR,
    raw_size_text      VARCHAR,           -- e.g. "16 oz", "per lb"
    category_hint      VARCHAR,
    coarse_category    VARCHAR,           -- assigned at ingest (silver.taxonomy); hard match guard
    canonical_id       BIGINT,            -- nullable until resolved
    match_confidence   DOUBLE,            -- 0..1
    match_method       VARCHAR,           -- exact|rapidfuzz|llm_adjudicator|manual
    resolved_at        TIMESTAMPTZ,
    first_seen_at      TIMESTAMPTZ,
    last_seen_at       TIMESTAMPTZ,
    bronze_manifest_id BIGINT             -- artifact this listing was first seen in
);

-- Prices: the fact table. One row per observed price for a source product.
CREATE TABLE IF NOT EXISTS prices (
    price_id           BIGINT PRIMARY KEY DEFAULT nextval('seq_price_id'),
    source_product_id  BIGINT NOT NULL,
    source             VARCHAR NOT NULL,
    store_id           VARCHAR,           -- nullable: region-level pricing
    region             VARCHAR,
    observed_at        TIMESTAMPTZ NOT NULL,
    ad_week            DATE,
    price              DECIMAL(10, 2),    -- listed / sale price
    regular_price      DECIMAL(10, 2),    -- nullable
    unit_price         DECIMAL(12, 4),    -- canonical $/unit
    unit               VARCHAR,           -- $/lb | $/oz | $/count | ...
    is_promo           BOOLEAN,
    discount_pct       DOUBLE,
    promo_text         VARCHAR,           -- "2 for $5", "BOGO", ...
    valid_from         DATE,
    valid_to           DATE,
    bronze_manifest_id BIGINT,
    confidence         DOUBLE             -- per-source confidence for this price
);

-- Migration for databases created before the coarse taxonomy (v2 step 3).
ALTER TABLE canonical_products ADD COLUMN IF NOT EXISTS coarse_category VARCHAR;
ALTER TABLE source_products ADD COLUMN IF NOT EXISTS coarse_category VARCHAR;

-- Human review queue: below-threshold entity matches never flow silently into
-- gold. The adjudicator agent parks ambiguous matches here.
CREATE TABLE IF NOT EXISTS resolution_queue (
    queue_id              BIGINT PRIMARY KEY DEFAULT nextval('seq_queue_id'),
    source_product_id     BIGINT NOT NULL,
    candidate_canonical_id BIGINT,        -- best guess, if any
    proposed_confidence   DOUBLE,
    reason                VARCHAR,
    status                VARCHAR NOT NULL DEFAULT 'open',  -- open|approved|rejected
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at           TIMESTAMPTZ,
    resolved_by           VARCHAR
);
