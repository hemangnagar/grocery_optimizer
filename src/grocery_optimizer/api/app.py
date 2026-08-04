"""FastAPI backend serving gold-layer queries to the PWA (v2 build step 5).

The frontend never computes prices: every number it renders comes from these
endpoints, which read gold views and the verdict engine only. Local-network
serving from the home server — no public exposure, no auth yet by design.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from .. import config
from ..silver.verdict import build_verdict

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"


def _connect() -> duckdb.DuckDBPyConnection:
    """Read-only connection per request; the API never writes."""
    return duckdb.connect(str(config.DB_PATH), read_only=True)


def _latest_basket(con: duckdb.DuckDBPyConnection) -> dict | None:
    row = con.execute(
        """
        SELECT basket_id, name, household_size, is_synthetic, created_at
        FROM baskets ORDER BY basket_id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    cols = ["basket_id", "name", "household_size", "is_synthetic", "created_at"]
    basket = dict(zip(cols, row))
    basket["created_at"] = basket["created_at"].isoformat()
    return basket


def _banners(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    return dict(con.execute("SELECT source, banner FROM sources").fetchall())


def create_app() -> FastAPI:
    app = FastAPI(title="Grocery Basket Optimizer", docs_url="/api/docs")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/verdict")
    def verdict() -> dict:
        con = _connect()
        try:
            basket = _latest_basket(con)
            if basket is None:
                raise HTTPException(
                    status_code=404,
                    detail="No basket found — run grocery-seed-demo or grocery-optimize first.",
                )
            result = build_verdict(con, basket["basket_id"])
            ad_week = con.execute(
                """
                SELECT max(g.ad_week)
                FROM gold_current_prices g
                JOIN basket_items bi ON bi.canonical_id = g.canonical_id
                WHERE bi.basket_id = ?
                """,
                [basket["basket_id"]],
            ).fetchone()[0]
            result["basket"] = basket
            result["banners"] = _banners(con)
            result["ad_week"] = ad_week.isoformat() if ad_week else None
            return result
        finally:
            con.close()

    @app.get("/api/basket")
    def basket_items() -> dict:
        """The basket's lines with the cheapest gold offer per item (the split)."""
        con = _connect()
        try:
            basket = _latest_basket(con)
            if basket is None:
                raise HTTPException(status_code=404, detail="No basket found.")
            rows = con.execute(
                """
                SELECT category, label, quantity, canonical_name, source,
                       price, line_cost, price_id
                FROM gold_basket_item_cost
                WHERE basket_id = ?
                ORDER BY category, label
                """,
                [basket["basket_id"]],
            ).fetchall()
            cols = ["category", "label", "quantity", "canonical_name", "source",
                    "price", "line_cost", "price_id"]
            return {
                "basket": basket,
                "banners": _banners(con),
                "lines": [dict(zip(cols, r)) for r in rows],
            }
        finally:
            con.close()

    # Static PWA last so /api/* wins routing.
    if WEBAPP_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEBAPP_DIR), html=True), name="webapp")
    return app


app = create_app()


def main() -> None:
    import os

    import uvicorn

    host = os.environ.get("GROCERY_HOST", "0.0.0.0")  # LAN by design (PWA on phone)
    port = int(os.environ.get("GROCERY_PORT", "8177"))
    print(f"Serving verdict PWA on http://{host}:{port} (gold-layer queries only)")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
