"""Data-contract verification: hold the live gold schema to the ODCS contract.

The contract (``contracts/gold_current_prices.odcs.yaml``, Open Data Contract
Standard v3) is the promise the gold layer makes to its consumers — the
verdict engine, the API, the narrator. This module makes the promise
enforceable: ``verify_contract`` compares the contract to the LIVE DuckDB
schema (names and physical types, both directions) and runs each declared
quality rule as a real query. Tests and ``grocery-verify-contract`` fail on
any violation, so the contract cannot drift into fiction.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import yaml

from .. import config

CONTRACTS_DIR = config.PROJECT_ROOT / "contracts"
GOLD_CONTRACT = CONTRACTS_DIR / "gold_current_prices.odcs.yaml"


def load_contract(path: Path = GOLD_CONTRACT) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _live_columns(con: duckdb.DuckDBPyConnection, relation: str) -> dict[str, str]:
    rows = con.execute(f"DESCRIBE {relation}").fetchall()
    return {name: col_type for name, col_type, *_ in rows}


def _check_quality_rule(con: duckdb.DuckDBPyConnection, rule: str) -> str | None:
    """Run one declared quality rule; return a violation string or None."""
    if rule == "trustGate":
        n = con.execute(
            "SELECT count(*) FROM gold_current_prices WHERE match_confidence < 0.85"
        ).fetchone()[0]
        return f"trustGate: {n} rows below 0.85 confidence" if n else None
    if rule == "latestOnly":
        n = con.execute(
            """
            SELECT count(*) FROM (
                SELECT source_product_id, store_id FROM gold_current_prices
                GROUP BY source_product_id, store_id HAVING count(*) > 1
            )
            """
        ).fetchone()[0]
        return f"latestOnly: {n} (source_product, store) pairs with >1 row" if n else None
    if rule == "radiusFilter":
        n = con.execute(
            """
            SELECT count(*) FROM gold_current_prices g
            JOIN home_location h ON true
            WHERE g.distance_miles IS NOT NULL AND g.distance_miles > h.radius_miles
            """
        ).fetchone()[0]
        return f"radiusFilter: {n} rows beyond the home radius" if n else None
    return f"unknown quality rule '{rule}' — contract declares a check the code lacks"


def verify_contract(
    con: duckdb.DuckDBPyConnection, path: Path = GOLD_CONTRACT
) -> list[str]:
    """Return violations (empty list = the live schema honors the contract)."""
    contract = load_contract(path)
    violations: list[str] = []

    for obj in contract.get("schema", []):
        relation = obj.get("physicalName") or obj["name"]
        live = _live_columns(con, relation)
        declared = {p["name"]: p for p in obj.get("properties", [])}

        for name, prop in declared.items():
            if name not in live:
                violations.append(f"{relation}: contract property '{name}' missing from view")
                continue
            expected = prop.get("physicalType")
            if expected and live[name] != expected:
                violations.append(
                    f"{relation}.{name}: type is {live[name]}, contract says {expected}"
                )
        for name in live:
            if name not in declared:
                violations.append(
                    f"{relation}: column '{name}' exists but is not in the contract"
                )

    for rule in contract.get("quality", []):
        problem = _check_quality_rule(con, rule["rule"])
        if problem:
            violations.append(problem)
    return violations


def main() -> None:
    """Entry point: verify the contract against the project database."""
    import sys

    from ..db import get_connection, init_db

    con = get_connection()
    try:
        init_db(con)
        violations = verify_contract(con)
    finally:
        con.close()
    if violations:
        print("CONTRACT VIOLATIONS:")
        for v in violations:
            print(f"  - {v}")
        sys.exit(1)
    print(f"Contract honored: {GOLD_CONTRACT.name} matches the live gold schema.")


if __name__ == "__main__":
    main()
