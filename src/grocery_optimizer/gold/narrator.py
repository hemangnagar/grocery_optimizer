"""Weekly plan narrator (agentic layer #3): gold facts -> audited prose.

The LLM narrates; it never computes. Mechanism:

1. ``build_facts`` queries the verdict engine + gold views into a fact pack:
   every total, delta, and count precomputed (so the model needs zero
   arithmetic), plus the gold row IDs behind them.
2. The writer (Claude, dependency-injected for tests) turns the fact pack into
   a short paragraph, instructed to use ONLY the provided numbers.
3. ``audit_narration`` — the deterministic gate — extracts every number from
   the prose and rejects the narration if any number is not in the fact pack.
   A failed audit raises; nothing unaudited is ever stored or served.
4. Stored in the ``narrations`` table with citations appended by the pipeline
   (the model never emits row IDs). The API serves only stored, audited rows:
   narrate once a week, serve cached — zero tokens per page view.
"""

from __future__ import annotations

import json
import os
import re
from typing import Callable

import duckdb

from .. import config
from ..silver.verdict import build_verdict

DEFAULT_MODEL = os.environ.get(
    "GROCERY_NARRATOR_MODEL",
    os.environ.get("GROCERY_ADJUDICATOR_MODEL", "claude-opus-4-8"),
)

_SYSTEM = (
    "You write the weekly one-paragraph grocery plan for a price-comparison "
    "app, from a JSON fact pack. HARD RULES:\n"
    "- Use ONLY numbers that appear in the fact pack, verbatim (dollar amounts "
    "to the cent, counts as given). NEVER compute, round, or invent a number.\n"
    "- Cover: the winning store and its total; the saving vs the runner-up (if "
    "any); the strongest coverage caveat (e.g. a cheap store missing items); "
    "and the multi-store split note.\n"
    "- 60-110 words, plain friendly prose, no markdown, no bullet points, no "
    "row IDs (citations are appended by the system)."
)

_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_DOLLAR_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")


class NarrationAuditError(RuntimeError):
    """The narration contained a number that is not in the fact pack."""


class NarratorError(RuntimeError):
    pass


# ---- fact pack -------------------------------------------------------------

def build_facts(con: duckdb.DuckDBPyConnection, basket_id: int) -> dict:
    """Deterministic fact pack: everything the narrator may say, precomputed."""
    verdict = build_verdict(con, basket_id)
    banners = dict(con.execute("SELECT source, banner FROM sources").fetchall())

    def store_fact(s: dict) -> dict:
        return {
            "store": banners.get(s["source"], s["source"]),
            "total": s["store_total"],
            "items_covered": s["items_covered"],
            "n_items": s["n_items"],
            "missing_count": len(s["missing_labels"]),
            "missing_labels": s["missing_labels"][:5],
            "full_coverage": s["full_coverage"],
        }

    flex = verdict["flexible"]
    winner = flex["winner"]
    facts: dict = {
        "n_items": verdict["n_items"],
        "winner": store_fact(winner) if winner else None,
        "other_stores": [
            store_fact(s) for s in flex["stores"]
            if not winner or s["source"] != winner["source"]
        ],
    }
    if winner:
        runner = next(
            (s for s in flex["stores"]
             if s["source"] != winner["source"] and s["full_coverage"]), None
        )
        if runner:
            facts["runner_up"] = store_fact(runner)
            facts["saving_vs_runner_up"] = round(
                runner["store_total"] - winner["store_total"], 2
            )
    fn = verdict["split_footnote"]
    if fn:
        facts["split"] = {
            "total": fn["split_total"],
            "n_stores": len(fn["stores_used"]),
            "stores": sorted(banners.get(s, s) for s in fn["stores_used"]),
            "extra_saving": fn["savings"],
            "worth_it": fn["worth_it"],
        }

    row = con.execute(
        """
        SELECT max(g.ad_week), list(DISTINCT g.price_id ORDER BY g.price_id)
        FROM gold_current_prices g
        JOIN basket_items bi ON bi.canonical_id = g.canonical_id
        WHERE bi.basket_id = ?
        """,
        [basket_id],
    ).fetchone()
    facts["ad_week"] = row[0].isoformat() if row[0] else None
    facts["_provenance"] = {
        "basket_id": basket_id,
        "price_ids": row[1] or [],
        "views": ["gold_current_prices", "gold_basket_item_cost"],
    }
    return facts


def _allowed_numbers(obj) -> set[float]:
    """Every numeric leaf in the fact pack except provenance row IDs."""
    out: set[float] = set()
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.add(round(float(obj), 2))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k != "_provenance":
                out |= _allowed_numbers(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out |= _allowed_numbers(v)
    return out


def audit_narration(narration: str, facts: dict) -> list[str]:
    """Return violations: numbers in the prose that gold cannot vouch for."""
    allowed = _allowed_numbers(facts)
    text = _DATE_RE.sub(" ", narration)
    violations = []
    for m in _DOLLAR_RE.finditer(text):
        value = round(float(m.group(1).replace(",", "")), 2)
        if value not in allowed:
            violations.append(f"${m.group(1)} not in fact pack")
    text = _DOLLAR_RE.sub(" ", text)
    for m in _NUMBER_RE.finditer(text):
        value = round(float(m.group(0)), 2)
        if value not in allowed:
            violations.append(f"{m.group(0)} not in fact pack")
    return violations


# ---- writer + persistence --------------------------------------------------

def make_claude_writer(model: str = DEFAULT_MODEL, api_key: str | None = None) -> Callable:
    """Return write(facts) -> narration text, backed by Claude."""
    import anthropic  # lazy so no-network tests don't require it

    key = api_key or config.get_anthropic_api_key()
    if not key:
        raise NarratorError(
            "ANTHROPIC_API_KEY is not set in the project .env "
            "(never set it globally — see CLAUDE.md)."
        )
    client = anthropic.Anthropic(api_key=key)

    def write(facts: dict) -> str:
        public = {k: v for k, v in facts.items() if k != "_provenance"}
        resp = client.messages.create(
            model=model,
            max_tokens=400,
            system=_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(public)}],
        )
        return next((b.text for b in resp.content if b.type == "text"), "").strip()

    return write


def _citations(facts: dict) -> str:
    prov = facts["_provenance"]
    ids = prov["price_ids"]
    shown = ",".join(str(i) for i in ids[:20]) + ("…" if len(ids) > 20 else "")
    return (
        f"basket {prov['basket_id']} · ad week {facts.get('ad_week')} · "
        f"{len(ids)} gold price rows (price_id {shown})"
    )


def narrate(
    con: duckdb.DuckDBPyConnection,
    basket_id: int,
    write: Callable,
    *,
    model: str | None = None,
) -> dict:
    """Fact pack -> LLM prose -> audit gate -> stored narration.

    Raises NarrationAuditError (storing nothing) if the prose contains any
    number the fact pack cannot vouch for — invented numbers never reach users.
    """
    facts = build_facts(con, basket_id)
    narration = write(facts)
    if not narration:
        raise NarratorError("writer returned empty narration")
    violations = audit_narration(narration, facts)
    if violations:
        raise NarrationAuditError("; ".join(violations))

    citations = _citations(facts)
    narration_id = con.execute(
        """
        INSERT INTO narrations (basket_id, ad_week, narration, citations, facts,
                                model, audit_ok)
        VALUES (?, ?, ?, ?, ?, ?, true)
        RETURNING narration_id
        """,
        [basket_id, facts.get("ad_week"), narration, citations,
         json.dumps(facts), model],
    ).fetchone()[0]
    return {
        "narration_id": narration_id,
        "narration": narration,
        "citations": citations,
        "audit_ok": True,
    }
