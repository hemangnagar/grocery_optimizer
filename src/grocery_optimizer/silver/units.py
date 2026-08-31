"""Canonical unit-price normalization.

Parses a size string (e.g. "1/2 gal", "19 oz", "13 fl oz", "12 ct", "per lb")
into a magnitude expressed in a canonical base unit, so prices from different
package sizes are comparable:

  - volume  -> base "fl_oz"  (gal=128, qt=32, pt=16, L=33.814, ml=0.033814)
  - weight  -> base "oz"     (lb=16, oz=1, kg=35.274, g=0.035274)
  - count   -> base "ct"     (ct/pk/each = magnitude, dozen = 12)

Multi-unit packs combine count and measure tokens: "1 oz 16 ct" is 16 oz of
product, "24 pack 16.9 fl oz" is 405.6 fl_oz, "2 pk 6 ct" is 12 ct. When both
a count and a weight/volume appear, the measure wins as the base unit and the
counts multiply into it; measure-only strings with several measure tokens keep
the FIRST (later ones usually restate the same size, e.g. "16 oz 1 lb").

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
    "dozen": 12.0, "doz": 12.0,
}

_MEASURES = (("fl_oz", _VOLUME), ("oz", _WEIGHT), ("ct", _COUNT))
# All tokens, longest first so "fl oz" wins over "oz" and "gallon" over "gal".
_ALL_TOKENS = sorted(
    {tok for _, m in _MEASURES for tok in m}, key=len, reverse=True
)
_TOKEN_ALT = "|".join(re.escape(t) for t in _ALL_TOKENS)
# magnitude: "1", "1.5", "1/2", or "1 1/2"; unit token follows, optionally
# hyphenated ("10-lb bag").
_SIZE_RE = re.compile(
    rf"(?P<mag>\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)[\s-]*(?P<unit>{_TOKEN_ALT})\b",
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


_HALF_RE = re.compile(r"\bhalf[\s-]+(?=gal|pint|quart|pound|dozen)")


def parse_size(text: str | None) -> dict | None:
    """Return {magnitude, unit, base_unit, base_qty[, pack_count]} or None.

    ``base_qty`` is the TOTAL package quantity in ``base_unit`` — pack
    multipliers included — so ``unit_price`` divides by what you actually
    take home.
    """
    if not text:
        return None
    lowered = _HALF_RE.sub("0.5 ", text.lower())

    measures: list[tuple[float, str, str, float]] = []  # (mag, unit, base_unit, base_qty)
    counts: list[float] = []  # count contribution, dozen-expanded
    for m in _SIZE_RE.finditer(lowered):
        base = _base_for(m.group("unit"))
        if base is None:
            continue
        base_unit, per_token = base
        magnitude = _parse_magnitude(m.group("mag"))
        if base_unit == "ct":
            counts.append(magnitude * per_token)
        else:
            measures.append(
                (magnitude, m.group("unit"), base_unit, magnitude * per_token)
            )

    if not measures and not counts:
        p = _PER_RE.search(lowered)
        if not p:
            return None
        base = _base_for(p.group("unit"))
        if base is None:
            return None
        base_unit, per_token = base
        return {
            "magnitude": 1.0,
            "unit": p.group("unit"),
            "base_unit": base_unit,
            "base_qty": per_token,
        }

    pack = 1.0
    for c in counts:
        pack *= c

    if measures:
        # Measure wins as the base; counts multiply in ("1 oz 16 ct" -> 16 oz).
        # Extra measure tokens are ignored: they restate the first.
        magnitude, unit, base_unit, base_qty = measures[0]
        result = {
            "magnitude": magnitude,
            "unit": unit,
            "base_unit": base_unit,
            "base_qty": base_qty * pack,
        }
        if counts:
            result["pack_count"] = pack
        return result

    # Count-only: "12 ct" -> 12; nested packs multiply ("2 pk 6 ct" -> 12).
    return {
        "magnitude": counts[0],
        "unit": "ct",
        "base_unit": "ct",
        "base_qty": pack,
    }


def unit_price(price: float | None, size_text: str | None) -> tuple[float | None, str | None]:
    """Return (unit_price, label) e.g. (0.0203, "$/fl_oz"), or (None, None)."""
    if price is None:
        return None, None
    parsed = parse_size(size_text)
    if not parsed or parsed["base_qty"] <= 0:
        return None, None
    return round(price / parsed["base_qty"], 4), f"$/{parsed['base_unit']}"
