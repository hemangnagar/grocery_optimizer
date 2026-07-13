"""Entry point: build or refresh the DuckDB bronze/silver/gold schema.

Idempotent — safe to run repeatedly. Usage::

    uv run grocery-init-db
"""

from __future__ import annotations

from .. import config
from ..db import init_db


def main() -> None:
    con = init_db()
    try:
        tables = con.execute(
            """
            SELECT table_type, table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
            ORDER BY table_type, table_name
            """
        ).fetchall()
        source_count = con.execute("SELECT count(*) FROM sources").fetchone()[0]
    finally:
        con.close()

    print(f"Schema ready at {config.DB_PATH}")
    for table_type, name in tables:
        print(f"  {table_type:<10} {name}")
    print(f"Seeded sources: {source_count}")


if __name__ == "__main__":
    main()
