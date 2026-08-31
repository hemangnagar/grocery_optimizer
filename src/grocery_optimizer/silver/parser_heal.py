"""Parser self-healing agent (agentic layer #1).

When a source changes its payload shape ("schema drift"), the builtin parser
raises or starts returning zero records. This agent:

1. DETECTS drift deterministically: a bronze manifest whose artifact makes the
   current parser raise, or parse to zero records while its structure diverges
   from the last known-good artifact for that source.
2. DIFFS the raw structure (JSON key paths, or an HTML tag histogram) between
   the known-good and drifted artifacts.
3. Asks Claude to PROPOSE a patched ``parse(raw)`` function (the proposer is
   dependency-injected, so tests run without network access).
4. VALIDATES the patch behind a deterministic gate: the patch must reproduce
   the current parser's output EXACTLY on the last known-good bronze artifact,
   and must produce schema-exact, non-empty records on the drifted one.
5. PROMOTES only gate-passing patches: code saved sha-addressed under
   ``data/parser_patches/`` and recorded in the ``parser_patches`` table.
   ``ingest_bronze`` overlays promoted patches (builtin stays the fallback).
   Rejected proposals are recorded too — the agent never silently retries.

The LLM writes code, but code never reaches ingest without passing the gate,
and patches never modify repo source — they are auditable data-layer artifacts
a human can inspect, fold into the codebase, or reject
(``UPDATE parser_patches SET status='rejected' WHERE patch_id=...``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Callable

import duckdb

from .. import config
from . import kcl, units

PATCH_DIR = config.DATA_DIR / "parser_patches"

# Every builtin parser returns records with EXACTLY these keys; the gate holds
# patches to the same contract so ingest never sees a shape it doesn't know.
RECORD_KEYS = frozenset(
    {
        "source", "source_sku", "raw_name", "raw_brand", "raw_size_text",
        "category_hint", "price", "regular_price", "unit_price", "unit",
        "is_promo", "discount_pct", "promo_text",
    }
)

# What patched code may use. No imports, no file or network access — parsers
# are pure raw-bytes -> records functions, and these cover the builtins' needs.
_SAFE_BUILTINS = {
    name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "enumerate", "float", "int",
        "isinstance", "len", "list", "max", "min", "range", "round", "set",
        "sorted", "str", "sum", "tuple", "zip",
        "Exception", "KeyError", "TypeError", "ValueError", "AttributeError",
        "IndexError",
    )
}

DEFAULT_MODEL = os.environ.get("GROCERY_HEALER_MODEL", "claude-opus-5")

_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["code", "notes"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You repair a broken grocery-data parser after upstream schema drift.\n\n"
    "You get: the current parser's Python source, a structural diff between "
    "the last payload it parsed correctly and the new payload it fails on, "
    "and a truncated sample of the new payload.\n\n"
    "Return a complete replacement as `code`: a single Python function\n"
    "    def parse(raw: bytes) -> list[dict]\n"
    "Hard requirements:\n"
    "- Same record contract as the current parser: every record has exactly "
    "the same keys, same value conventions (price None when absent, etc.).\n"
    "- On a payload shaped like the OLD structure, parse must return exactly "
    "what the current parser returns (it is validated on the last known-good "
    "file byte-for-byte; support both shapes if needed).\n"
    "- No import statements and no I/O. Pre-provided in scope: `json`, `re`, "
    "`BeautifulSoup`, `unit_price(price, size_text)`, and `parse_deals(raw)`. "
    "Standard builtins like len/float/sorted are available.\n"
    "- Helper functions are allowed, but `parse` must be defined.\n"
    "`notes` = one short line on what drifted and how the patch handles it."
)


def _exec_namespace() -> dict:
    from bs4 import BeautifulSoup  # heavier import kept local

    return {
        "__builtins__": dict(_SAFE_BUILTINS),
        "json": json,
        "re": re,
        "BeautifulSoup": BeautifulSoup,
        "unit_price": units.unit_price,
        "parse_deals": kcl.parse_deals,
    }


# -- structure summaries -----------------------------------------------------

def _json_paths(node, prefix: str = "", depth: int = 0, out: set | None = None) -> set:
    out = set() if out is None else out
    if depth > 6:
        return out
    if isinstance(node, dict):
        for key, value in node.items():
            _json_paths(value, f"{prefix}.{key}", depth + 1, out)
    elif isinstance(node, list):
        out.add(f"{prefix}[]")
        for item in node[:3]:
            _json_paths(item, f"{prefix}[]", depth + 1, out)
    else:
        out.add(f"{prefix}: {type(node).__name__}")
    return out


def describe_structure(raw: bytes) -> set[str]:
    """Structure fingerprint: JSON key paths, or an HTML tag/class histogram."""
    try:
        return _json_paths(json.loads(raw))
    except (ValueError, UnicodeDecodeError):
        pass
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    counts: dict[str, int] = {}
    for el in soup.find_all(True):
        classes = ".".join(sorted(el.get("class") or []))
        key = f"<{el.name}{'.' + classes if classes else ''}>"
        counts[key] = counts.get(key, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:40]
    return {f"{k} x{v}" for k, v in top}


def structure_diff(known_good_raw: bytes, drifted_raw: bytes) -> str:
    old = describe_structure(known_good_raw)
    new = describe_structure(drifted_raw)
    removed = sorted(old - new)
    added = sorted(new - old)
    lines = [f"- REMOVED: {p}" for p in removed] + [f"+ ADDED:   {p}" for p in added]
    return "\n".join(lines) if lines else "(no structural difference found)"


# -- drift detection ---------------------------------------------------------

def _manifests(con, source: str, statuses: tuple[str, ...]) -> list[dict]:
    rows = con.execute(
        """
        SELECT manifest_id, raw_path FROM bronze_manifest
        WHERE source = ? AND parse_status IN ({})
        ORDER BY manifest_id DESC
        """.format(",".join("?" * len(statuses))),
        [source, *statuses],
    ).fetchall()
    return [{"manifest_id": m, "raw_path": p} for m, p in rows]


def _read(raw_path: str) -> bytes:
    return (config.BRONZE_DIR / raw_path).read_bytes()


def detect_drift(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Find (source, drifted artifact, known-good artifact) triples.

    Drift = the CURRENT parser raises on the artifact, or returns zero records
    while the artifact carries structure the known-good payload does NOT
    (added paths = a renamed/moved container). A legitimately empty payload
    (e.g. ``{"data": []}``) only REMOVES paths, so it is never flagged.
    Known-good = newest 'parsed' artifact that still parses to records today.
    """
    from .normalize import BUILTIN_PARSERS

    candidates = []
    for source, parser in BUILTIN_PARSERS.items():
        known_good = None
        for m in _manifests(con, source, ("parsed",)):
            try:
                if parser(_read(m["raw_path"])):
                    known_good = m
                    break
            except Exception:
                continue
        if known_good is None:
            continue  # nothing trustworthy to validate against — skip source
        good_raw = _read(known_good["raw_path"])

        for m in _manifests(con, source, ("pending", "error", "empty")):
            try:
                raw = _read(m["raw_path"])
            except OSError:
                continue
            try:
                records = parser(raw)
                added_paths = describe_structure(raw) - describe_structure(good_raw)
                is_drift = not records and bool(added_paths)
            except Exception:
                is_drift = True
            if is_drift:
                candidates.append(
                    {
                        "source": source,
                        "drift_manifest_id": m["manifest_id"],
                        "drift_raw": raw,
                        "known_good_manifest_id": known_good["manifest_id"],
                        "known_good_raw": good_raw,
                    }
                )
                break  # one representative drifted artifact per source
    return candidates


# -- proposal (LLM, dependency-injected) -------------------------------------

def make_claude_proposer(model: str = DEFAULT_MODEL, api_key: str | None = None) -> Callable:
    """Return propose(parser_source, diff, sample) -> {code, notes}."""
    import anthropic  # lazy so no-network tests don't require it

    key = api_key or config.get_anthropic_api_key()
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set in the project .env "
            "(never set it globally — see CLAUDE.md)."
        )
    client = anthropic.Anthropic(api_key=key)

    def propose(parser_source: str, diff: str, sample: str) -> dict:
        user = (
            "CURRENT PARSER SOURCE:\n```python\n" + parser_source + "\n```\n\n"
            "STRUCTURE DIFF (last known-good payload -> new payload):\n"
            + diff
            + "\n\nNEW PAYLOAD SAMPLE (truncated):\n" + sample
        )
        resp = client.messages.create(
            model=model,
            max_tokens=16000,
            system=_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": _PROPOSAL_SCHEMA}},
            messages=[{"role": "user", "content": user}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("model declined the patch request")
        text = next((b.text for b in resp.content if b.type == "text"), "{}")
        data = json.loads(text)
        return {"code": str(data.get("code", "")), "notes": str(data.get("notes", ""))[:500]}

    return propose


# -- deterministic validation gate -------------------------------------------

def validate_patch(
    code: str,
    current_parser: Callable,
    known_good_raw: bytes,
    drifted_raw: bytes,
) -> tuple[bool, str]:
    """The gate. Returns (ok, reason). Nothing about this is model-dependent."""
    if re.search(r"^\s*(import|from)\s", code, re.MULTILINE):
        return False, "patch contains an import statement"
    ns = _exec_namespace()
    try:
        exec(compile(code, "<parser_patch>", "exec"), ns)  # noqa: S102 — gated below
    except Exception as exc:
        return False, f"patch does not compile/execute: {exc}"
    fn = ns.get("parse")
    if not callable(fn):
        return False, "patch defines no parse(raw) function"

    try:
        expected = current_parser(known_good_raw)
        got = fn(known_good_raw)
    except Exception as exc:
        return False, f"patch raised on the known-good artifact: {exc}"
    if got != expected:
        return False, "patch output diverges from current parser on known-good bronze"

    try:
        new_records = fn(drifted_raw)
    except Exception as exc:
        return False, f"patch raised on the drifted artifact: {exc}"
    if not new_records:
        return False, "patch still returns zero records for the drifted artifact"
    for i, rec in enumerate(new_records):
        if not isinstance(rec, dict) or set(rec) != RECORD_KEYS:
            return False, f"record {i} does not match the record contract keys"
        if not rec.get("raw_name"):
            return False, f"record {i} has no raw_name"
        if rec["price"] is not None and not isinstance(rec["price"], (int, float)):
            return False, f"record {i} price is not numeric/None"
    return True, f"gate passed: {len(new_records)} records from drifted artifact"


# -- promotion + loading -----------------------------------------------------

def _store_patch(con, source: str, code: str | None, status: str, reason: str,
                 diff: str, drift_mid: int, good_mid: int, model: str | None) -> int:
    sha = hashlib.sha256(code.encode()).hexdigest() if code else None
    rel_path = None
    if code and status == "promoted":
        PATCH_DIR.mkdir(parents=True, exist_ok=True)
        rel_path = f"{source}_{sha[:12]}.py"
        (PATCH_DIR / rel_path).write_text(code, encoding="utf-8")
    return con.execute(
        """
        INSERT INTO parser_patches (source, status, reason, code_sha256, code_path,
                                    structure_diff, drift_manifest_id,
                                    known_good_manifest_id, model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING patch_id
        """,
        [source, status, reason, sha, rel_path, diff, drift_mid, good_mid, model],
    ).fetchone()[0]


def load_promoted_parser(con: duckdb.DuckDBPyConnection, source: str) -> Callable | None:
    """Latest promoted patch for a source, sha-verified, or None."""
    row = con.execute(
        """
        SELECT code_path, code_sha256 FROM parser_patches
        WHERE source = ? AND status = 'promoted' AND code_path IS NOT NULL
        ORDER BY patch_id DESC LIMIT 1
        """,
        [source],
    ).fetchone()
    if row is None:
        return None
    path, sha = row
    try:
        code = (PATCH_DIR / path).read_text(encoding="utf-8")
    except OSError:
        return None
    if hashlib.sha256(code.encode()).hexdigest() != sha:
        return None  # tampered/corrupted patch never runs
    ns = _exec_namespace()
    try:
        exec(compile(code, f"<parser_patch:{path}>", "exec"), ns)  # noqa: S102
    except Exception:
        return None
    fn = ns.get("parse")
    return fn if callable(fn) else None


def heal(
    con: duckdb.DuckDBPyConnection,
    propose: Callable,
    *,
    model: str | None = None,
    force: bool = False,
) -> list[dict]:
    """Detect -> propose -> gate -> promote/reject. Returns per-source results."""
    import inspect

    from .normalize import BUILTIN_PARSERS

    results = []
    for cand in detect_drift(con):
        source = cand["source"]
        if not force:
            seen = con.execute(
                "SELECT count(*) FROM parser_patches WHERE drift_manifest_id = ?",
                [cand["drift_manifest_id"]],
            ).fetchone()[0]
            if seen:
                results.append({"source": source, "status": "skipped",
                                "reason": "drift artifact already has a patch attempt"})
                continue

        parser = BUILTIN_PARSERS[source]
        diff = structure_diff(cand["known_good_raw"], cand["drift_raw"])
        sample = cand["drift_raw"][:4000].decode("utf-8", errors="replace")
        try:
            proposal = propose(inspect.getsource(parser), diff, sample)
        except Exception as exc:
            results.append({"source": source, "status": "error",
                            "reason": f"proposer failed: {exc}"})
            continue

        ok, reason = validate_patch(
            proposal["code"], parser, cand["known_good_raw"], cand["drift_raw"]
        )
        status = "promoted" if ok else "rejected"
        patch_id = _store_patch(
            con, source, proposal["code"], status,
            f"{reason} | {proposal['notes']}", diff,
            cand["drift_manifest_id"], cand["known_good_manifest_id"], model,
        )
        results.append({"source": source, "status": status,
                        "reason": reason, "patch_id": patch_id})
    return results
