"""DuckDB connection and schema initialization.

The schema is defined in ordered .sql files under ``sql/`` and applied
idempotently: tables use ``CREATE ... IF NOT EXISTS``, views use
``CREATE OR REPLACE VIEW``, so ``init_db`` is safe to re-run.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from . import config

# Applied in order. Bronze first (manifest), then silver (products/prices),
# then gold (views that read silver).
SCHEMA_FILES = ("01_bronze.sql", "02_silver.sql", "03_gold.sql", "04_basket.sql")


def get_connection(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open a connection to the project DuckDB file.

    Ensures the containing directory exists first (read_only connections
    require the file to already exist).
    """
    if not read_only:
        config.ensure_dirs()
    return duckdb.connect(str(config.DB_PATH), read_only=read_only)


def apply_sql_file(con: duckdb.DuckDBPyConnection, path: Path) -> None:
    """Execute all statements in a .sql file against the connection."""
    con.execute(path.read_text(encoding="utf-8"))


def init_db(con: duckdb.DuckDBPyConnection | None = None) -> duckdb.DuckDBPyConnection:
    """Create (or refresh) the full bronze/silver/gold schema.

    If ``con`` is None a connection to the project DB is opened and returned;
    pass an existing connection (e.g. an in-memory one) for tests.
    """
    config.ensure_dirs()
    owns_con = con is None
    if owns_con:
        con = get_connection()
    for name in SCHEMA_FILES:
        apply_sql_file(con, config.SQL_DIR / name)
    return con
