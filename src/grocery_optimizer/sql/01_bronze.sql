-- Bronze layer: manifest of every raw fetch artifact.
--
-- The raw bytes (JSON/HTML) live on disk under data/bronze/ verbatim — this is
-- the time machine, since weekly ads expire and are unrecoverable. DuckDB only
-- stores metadata pointing at those files; never parse-and-discard.

CREATE SEQUENCE IF NOT EXISTS seq_manifest_id START 1;

CREATE TABLE IF NOT EXISTS bronze_manifest (
    manifest_id     BIGINT PRIMARY KEY DEFAULT nextval('seq_manifest_id'),
    source          VARCHAR NOT NULL,   -- kroger|wholefoods|lidl|aldi_kcl|giant|safeway
    source_kind     VARCHAR NOT NULL,   -- api|scrape|aggregator
    fetched_at      TIMESTAMPTZ NOT NULL,
    ad_week         DATE,               -- Wednesday-start week the ad applies to
    region          VARCHAR,
    store_id        VARCHAR,            -- nullable: region-level fetches have no store
    request_url     VARCHAR,
    request_params  JSON,
    http_status     INTEGER,
    content_type    VARCHAR,
    raw_path        VARCHAR NOT NULL,   -- path relative to data/bronze/
    content_sha256  VARCHAR,            -- integrity + dedupe
    byte_size       BIGINT,
    parse_status    VARCHAR NOT NULL DEFAULT 'pending',  -- pending|parsed|failed|skipped
    parser_version  VARCHAR,
    notes           VARCHAR
);
