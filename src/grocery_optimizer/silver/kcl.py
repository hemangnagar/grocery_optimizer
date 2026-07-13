"""Parse KCL Aldi deal HTML (bronze) into structured deal records.

KCL embeds deal data in the page as an HTML-entity-encoded React "Flight"
payload where every value is wrapped in a type tuple, e.g.::

    "name":[0,"Cucumber"],"regularPrice":[0,0.75],"discountPrice":[0,0.45],
    "url":[0,"https://www.aldi.us/store/aldi/products/3253345-cucumber-each"]

We unescape the entities, split the blob on product boundaries (each product
object begins with ``"_id":[0,"``), and pull fields individually so that deals
missing an optional field (e.g. no regularPrice) still parse. The Aldi SKU in
the product URL and the KCL ``_id`` are the join keys for entity resolution.
"""

from __future__ import annotations

import html
import re

_PRODUCT_SPLIT = '"_id":[0,"'
_RE_NAME = re.compile(r'"name":\[0,"((?:[^"\\]|\\.)*)"\]')
_RE_REGULAR = re.compile(r'"regularPrice":\[0,([\d.]+)\]')
_RE_DISCOUNT = re.compile(r'"discountPrice":\[0,([\d.]+)\]')
_RE_DTYPE = re.compile(r'"discountType":\[0,"([^"]*)"\]')
_RE_QTY = re.compile(r'"quantity":\[0,([\d.]+)\]')
_RE_URL = re.compile(r'"url":\[0,"([^"]*)"\]')
_RE_ALDI_SKU = re.compile(r"/products/(\d+)-")


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_deals(content: bytes | str, *, store: str = "aldi") -> list[dict]:
    """Extract structured Aldi deal records from a KCL page.

    Each dict: ``kcl_id``, ``name``, ``price`` (deal price), ``regular_price``
    (or None), ``discount_pct`` (computed, or None), ``discount_type``,
    ``quantity``, ``url``, ``source_sku`` (Aldi SKU from the URL), ``store``.
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    text = html.unescape(content)

    deals: list[dict] = []
    seen: set[str] = set()
    # First split element is the preamble before any product object.
    for chunk in text.split(_PRODUCT_SPLIT)[1:]:
        # A product deal always carries a discountPrice; skip store/other nodes.
        if '"discountPrice"' not in chunk:
            continue
        end = chunk.find('"')
        if end <= 0:
            continue
        kcl_id = chunk[:end]

        name_m = _RE_NAME.search(chunk)
        disc_m = _RE_DISCOUNT.search(chunk)
        if not name_m or not disc_m:
            continue

        price = _to_float(disc_m.group(1))
        regular = _to_float(_RE_REGULAR.search(chunk).group(1)) if _RE_REGULAR.search(chunk) else None
        url_m = _RE_URL.search(chunk)
        url = url_m.group(1) if url_m else None
        sku_m = _RE_ALDI_SKU.search(url) if url else None
        qty_m = _RE_QTY.search(chunk)
        dtype_m = _RE_DTYPE.search(chunk)

        discount_pct = None
        if regular and price is not None and regular > 0:
            discount_pct = round((regular - price) / regular * 100)

        dedupe_key = f"{kcl_id}|{name_m.group(1)}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        deals.append(
            {
                "kcl_id": kcl_id,
                "name": name_m.group(1),
                "price": price,
                "regular_price": regular,
                "discount_pct": discount_pct,
                "discount_type": dtype_m.group(1) if dtype_m else None,
                "quantity": _to_float(qty_m.group(1)) if qty_m else None,
                "url": url,
                "source_sku": sku_m.group(1) if sku_m else None,
                "store": store,
            }
        )
    return deals
