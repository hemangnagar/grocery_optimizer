"""Canonical unit-price normalization.

Parses a size string (e.g. "1/2 gal", "19 oz", "13 fl oz", "12 ct", "per lb")
into a magnitude expressed in a canonical base unit, so prices from different
package sizes are comparable:

  - volume  -> base "fl_oz"  (gal=128, qt=32, pt=16, L=33.814, ml=0.033814)
  - weight  -> base "oz"     (lb=16, oz=1, kg=35.274, g=0.035274)
  - count   -> base "ct"     (ct/pk/each = magnitude)

Ambiguity note: a bare "oz" is treated as WEIGHT; fluid volume must say "fl oz".
"""

from __future__ import annotations

import re

# token -> quantity of the base unit that one token-unit represents
_VOLUME = {
    "fl oz": 1.0, "floz": 1.0, "fl. oz": 1.0, "fluid ounce": 1.0, "fluid ounces": 1.0,
    "pt": 16.0, "pint": 16.0, "pints": 16.0,
    "qt": 32.0, "quart": 32.0, "quarts": 32.0,
    "gal": 128.0, "gallon": 128.0, "gallons": 128.0,
    "l": 33.814, "liter": 33.814, "litre": 33.814, "liters": 33.814,
    "ml": 0.033814, "milliliter": 0.033814, "milliliters": 0.033814,
}
_WEIGHT = {
    "oz": 1.0, "ounce": 1.0, "ounces": 1.0,
    "lb": 16.0, "lbs": 16.0, "pound": 16.0, "pounds": 16.0,
    "g": 0.035274, "gram": 0.035274, "grams": 0.035274,
    "kg": 35.274, "kilogram": 35.274, "kilograms": 35.274,
}
_COUNT = {
    "ct": 1.0, "count": 1.0, "cnt": 1.0,
    "pk": 1.0, "pack": 1.0, "pks": 1.0,
    "ea": 1.0, "each": 1.0,
}

_MEASURES = (("fl_oz", _VOLUME), ("oz", _WEIGHT), ("ct", _COUNT))
# All tokens, longest first so "fl oz" wins over "oz" and "gallon" over "gal".
_ALL_TOKENS = sorted(
    {tok for _, m in _MEASURES for tok in m}, key=len, reverse=True
)
_TOKEN_ALT = "|".join(re.escape(t) for t in _ALL_TOKENS)
# magnitude: "1", "1.5", "1/2", or "1 1/2"; unit token follows.
_SIZE_RE = re.compile(
    rf"(?P<mag>\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)\s*(?P<unit>{_TOKEN_ALT})\b",
    re.IGNORECASE,
)
# "per lb" / "per each" style (magnitude implicitly 1)
_PER_RE = re.compile(rf"per\s+(?P<unit>{_TOKEN_ALT})\b", re.IGNORECASE)


def _parse_magnitude(token: str) -> float:
    token = token.strip()
    total = 0.0
    for part in token.split():
        if "/" in part:
            num, den = part.split("/")
            total += float(num) / float(den)
        else:
            total += float(part)
    return total


def _base_for(unit: str):
    unit = unit.lower()
    for base_unit, mapping in _MEASURES:
        if unit in mapping:
            return base_unit, mapping[unit]
    return None


def parse_size(text: str | None) -> dict | None:
    """Return {magnitude, unit, base_unit, base_qty} or None if unparseable."""
    if not text:
        return None
    lowered = text.lower()
    m = _SIZE_RE.search(lowered)
    if m:
        magnitude = _parse_magnitude(m.group("mag"))
        unit = m.group("unit")
    else:
        p = _PER_RE.search(lowered)
        if not p:
            return None
        magnitude = 1.0
        unit = p.group("unit")
    base = _base_for(unit)
    if base is None:
        return None
    base_unit, per_token = base
    return {
        "magnitude": magnitude,
        "unit": unit,
        "base_unit": base_unit,
        "base_qty": magnitude * per_token,
    }


def unit_price(price: float | None, size_text: str | None) -> tuple[float | None, str | None]:
    """Return (unit_price, label) e.g. (0.0203, "$/fl_oz"), or (None, None)."""
    if price is None:
        return None, None
    parsed = parse_size(size_text)
    if not parsed or parsed["base_qty"] <= 0:
        return None, None
    return round(price / parsed["base_qty"], 4), f"$/{parsed['base_unit']}"
