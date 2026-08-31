"""Entry point: parser self-healing agent (agentic layer #1).

Detects schema drift in captured bronze artifacts, asks Claude to propose a
patched parser, and promotes it only if the deterministic validation gate
passes (see silver.parser_heal). Spends API credits unless --dry-run. Usage::

    uv run grocery-heal-parsers [--dry-run] [--force]

--dry-run  detect and report drift only (zero tokens)
--force    re-propose even for drift artifacts that already have a patch row
"""

from __future__ import annotations

import sys

from ..db import get_connection, init_db
from ..silver.parser_heal import DEFAULT_MODEL, detect_drift, heal, make_claude_proposer


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv

    con = get_connection()
    try:
        init_db(con)
        if dry_run:
            drifted = detect_drift(con)
            if not drifted:
                print("No parser drift detected.")
                return
            for c in drifted:
                print(
                    f"DRIFT: {c['source']} — manifest {c['drift_manifest_id']} fails "
                    f"the current parser (known-good: {c['known_good_manifest_id']})."
                )
            print("\nRun without --dry-run to propose patches (spends API credits).")
            return

        propose = make_claude_proposer()
        results = heal(con, propose, model=DEFAULT_MODEL, force=force)
        if not results:
            print("No parser drift detected — nothing to heal.")
            return
        for r in results:
            line = f"{r['source']:<12} {r['status']:<9} {r['reason']}"
            if r.get("patch_id"):
                line += f" (patch_id={r['patch_id']})"
            print(line)
        if any(r["status"] == "promoted" for r in results):
            print("\nPromoted patches apply on the next grocery-normalize run.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
