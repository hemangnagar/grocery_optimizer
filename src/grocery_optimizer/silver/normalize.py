"""Normalize bronze artifacts into silver ``source_products`` + ``prices``.

Parses captured Kroger product JSON and KCL Aldi deal HTML into rows with
canonical unit prices, then marks the bronze manifest ``parsed``. Entity
resolution to canonical products is a separate step (silver.resolve).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import duckdb

from .. import config
from .kcl import parse_deals
from .units import unit_price

# Per-source price confidence (feeds the gold >=0.85 trust gate).
_SOURCE_CONFIDENCE = {"kroger": 0.99, "wholefoods": 0.90, "aldi_kcl": 0.80}

_TRADEMARK = re.compile(r"[®™©]")  # ® ™ ©
_PARENS = re.compile(r"\([^)]*\)")
_SIZE_TOKENS = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:fl\s*oz|oz|lb|lbs|pound|gal|gallon|qt|pt|ml|l|ct|count|pk|pack|g|kg|ea|each)\b",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def normalize_name(raw: str | None) -> str:
    """Lowercased, trademark/size/punctuation-stripped name for fuzzy matching."""
    if not raw:
        return ""
    text = _TRADEMARK.sub("", raw).lower()
    text = _PARENS.sub(" ", text)
    text = _SIZE_TOKENS.sub(" ", text)
    text = _NON_ALNUM.sub(" ", text)
    return _WS.sub(" ", text).strip()


def _ad_week(dt: datetime) -> datetime.date:
    """Wednesday-start of the week containing ``dt`` (ads refresh Wednesdays)."""
    # Monday=0 .. Sunday=6; Wednesday=2.
    days_since_wed = (dt.weekday() - 2) % 7
    return (dt - timedelta(days=days_since_wed)).date()


def parse_kroger_products(raw: bytes) -> list[dict]:
    """One record per Kroger product (uses the first item for size/price)."""
    payload = json.loads(raw)
    records: list[dict] = []
    for product in payload.get("data", []):
        items = product.get("items") or []
        item = items[0] if items else {}
        price_block = item.get("price") or {}
        regular = price_block.get("regular")
        promo = price_block.get("promo")
        is_promo = bool(promo) and promo > 0
        price = promo if is_promo else regular
        size_text = item.get("size")
        up, unit = unit_price(price, size_text)
        cats = product.get("categories") or []
        discount_pct = (
            round((regular - promo) / regular * 100)
            if is_promo and regular
            else None
        )
        records.append(
            {
                "source": "kroger",
                "source_sku": product.get("productId"),
                "raw_name": product.get("description"),
                "raw_brand": product.get("brand"),
                "raw_size_text": size_text,
                "category_hint": cats[0] if cats else None,
                "price": price,
                "regular_price": regular if is_promo else None,
                "unit_price": up,
                "unit": unit,
                "is_promo": is_promo,
                "discount_pct": discount_pct,
                "promo_text": None,
            }
        )
    return records


def parse_kcl_records(raw: bytes) -> list[dict]:
    """One record per KCL Aldi deal (size is embedded in the product name)."""
    records: list[dict] = []
    for deal in parse_deals(raw):
        up, unit = unit_price(deal.get("price"), deal.get("name"))
        records.append(
            {
                "source": "aldi_kcl",
                "source_sku": deal.get("source_sku"),
                "raw_name": deal.get("name"),
                "raw_brand": None,
                "raw_size_text": deal.get("name"),
                "category_hint": None,
                "price": deal.get("price"),
                "regular_price": deal.get("regular_price"),
                "unit_price": up,
                "unit": unit,
                "is_promo": True,
                "discount_pct": deal.get("discount_pct"),
                "promo_text": deal.get("discount_type"),
            }
        )
    return records


def parse_wfm_products(raw: bytes) -> list[dict]:
    """One record per Whole Foods product. Price is per unit-of-measure (uom),
    so we express size as "1 <uom>" and let units.unit_price convert to a base."""
    payload = json.loads(raw)
    # Category from the breadcrumb (last labeled crumb), for match gating.
    category = None
    breadcrumb = payload.get("breadcrumb") or []
    if isinstance(breadcrumb, list) and breadcrumb:
        last = breadcrumb[-1]
        if isinstance(last, dict):
            category = last.get("label")

    records: list[dict] = []
    for product in payload.get("results", []):
        regular = product.get("regularPrice")
        sale = product.get("salePrice")
        is_promo = bool(sale) and sale > 0 and (not regular or sale < regular)
        price = sale if is_promo else regular
        uom = product.get("uom")
        up, unit = unit_price(price, f"1 {uom}" if uom else None)
        discount_pct = (
            round((regular - sale) / regular * 100) if is_promo and regular else None
        )
        records.append(
            {
                "source": "wholefoods",
                "source_sku": product.get("slug"),
                "raw_name": product.get("name"),
                "raw_brand": product.get("brand"),
                "raw_size_text": uom,
                "category_hint": category,
                "price": price,
                "regular_price": regular if is_promo else None,
                "unit_price": up,
                "unit": unit,
                "is_promo": is_promo,
                "discount_pct": discount_pct,
                "promo_text": None,
            }
        )
    return records


def _upsert_source_product(con, rec: dict, fetched_at, manifest_id: int) -> int:
    existing = None
    if rec["source_sku"]:
        existing = con.execute(
            "SELECT source_product_id FROM source_products WHERE source = ? AND source_sku = ?",
            [rec["source"], rec["source_sku"]],
        ).fetchone()
    if existing:
        spid = existing[0]
        con.execute(
            """
            UPDATE source_products
            SET last_seen_at = ?, raw_name = ?, raw_size_text = ?,
                category_hint = COALESCE(category_hint, ?)
            WHERE source_product_id = ?
            """,
            [fetched_at, rec["raw_name"], rec["raw_size_text"], rec["category_hint"], spid],
        )
        return spid
    return con.execute(
        """
        INSERT INTO source_products (
            source, source_sku, raw_name, raw_brand, raw_size_text,
            category_hint, first_seen_at, last_seen_at, bronze_manifest_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING source_product_id
        """,
        [
            rec["source"], rec["source_sku"], rec["raw_name"], rec["raw_brand"],
            rec["raw_size_text"], rec["category_hint"], fetched_at, fetched_at,
            manifest_id,
        ],
    ).fetchone()[0]


def _insert_price(con, spid: int, rec: dict, manifest, fetched_at) -> None:
    con.execute(
        """
        INSERT INTO prices (
            source_product_id, source, store_id, region, observed_at, ad_week,
            price, regular_price, unit_price, unit, is_promo, discount_pct,
            promo_text, bronze_manifest_id, confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            spid, rec["source"], manifest["store_id"], manifest["region"],
            fetched_at, _ad_week(fetched_at), rec["price"], rec["regular_price"],
            rec["unit_price"], rec["unit"], rec["is_promo"], rec["discount_pct"],
            rec["promo_text"], manifest["manifest_id"],
            _SOURCE_CONFIDENCE.get(rec["source"], 0.75),
        ],
    )


def _pending_manifests(con) -> list[dict]:
    rows = con.execute(
        """
        SELECT manifest_id, source, raw_path, fetched_at, store_id, region
        FROM bronze_manifest
        WHERE parse_status = 'pending'
          AND (
                (source = 'kroger'
                 AND json_extract_string(request_params, '$.endpoint') = 'products')
             OR source = 'aldi_kcl'
             OR source = 'wholefoods'
          )
        ORDER BY manifest_id
        """
    ).fetchall()
    cols = ["manifest_id", "source", "raw_path", "fetched_at", "store_id", "region"]
    return [dict(zip(cols, r)) for r in rows]


def ingest_bronze(con: duckdb.DuckDBPyConnection) -> dict:
    """Parse all pending product/deal bronze artifacts into silver.

    Returns counts: ``{manifests, source_products, prices}``.
    """
    parsers = {
        "kroger": parse_kroger_products,
        "aldi_kcl": parse_kcl_records,
        "wholefoods": parse_wfm_products,
    }
    counts = {"manifests": 0, "source_products": 0, "prices": 0}

    for manifest in _pending_manifests(con):
        raw = (config.BRONZE_DIR / manifest["raw_path"]).read_bytes()
        fetched_at = manifest["fetched_at"] or datetime.now(timezone.utc)
        records = parsers[manifest["source"]](raw)
        for rec in records:
            spid = _upsert_source_product(con, rec, fetched_at, manifest["manifest_id"])
            _insert_price(con, spid, rec, manifest, fetched_at)
            counts["prices"] += 1
        con.execute(
            "UPDATE bronze_manifest SET parse_status = 'parsed', parser_version = 'v1' "
            "WHERE manifest_id = ?",
            [manifest["manifest_id"]],
        )
        counts["manifests"] += 1

    counts["source_products"] = con.execute(
        "SELECT count(*) FROM source_products"
    ).fetchone()[0]
    return counts
