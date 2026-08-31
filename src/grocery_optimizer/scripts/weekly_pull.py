"""Entry point: the Tuesday-night ingestion run (v2 build step 6).

Chains every fetcher plus normalization with PER-SOURCE ERROR ISOLATION: one
chain's outage (or missing credentials) never blocks the others, and bronze
capture still happens for whatever succeeded. Ads refresh Wednesdays, so the
Windows Task Scheduler job (see ``ops/register_weekly_pull.ps1``) runs this
Tuesday night. Also fine to run by hand::

    uv run grocery-weekly-pull [--skip kroger,traderjoes]

LLM steps (grocery-adjudicate*, grocery-narrate) are deliberately NOT part of
the scheduled pull — they spend tokens and their cadence is a human choice.
Each run appends a summary line per stage to ``data/logs/weekly_pull.log``.
"""

from __future__ import annotations

import sys
import time
import traceback
from datetime import datetime, timezone

from .. import config

LOG_DIR = config.DATA_DIR / "logs"


def _stages() -> list[tuple[str, object]]:
    # Imported lazily so one script's import-time problem (e.g. Playwright
    # missing for Trader Joe's) can't take down the whole run.
    def stage(module_name: str):
        def run() -> None:
            module = __import__(
                f"grocery_optimizer.scripts.{module_name}", fromlist=["main"]
            )
            module.main()

        return run

    return [
        ("kroger_locations", stage("kroger_locations")),  # store lat/lon refresh
        ("kroger", stage("kroger_fetch")),
        ("wholefoods", stage("wfm_fetch")),
        ("traderjoes", stage("tj_fetch")),
        ("aldi_kcl", stage("kcl_fetch")),
        ("lidl_probe", stage("lidl_probe")),
        ("normalize", stage("normalize")),
    ]


def _log(line: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with (LOG_DIR / "weekly_pull.log").open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp} {line}\n")


def run_weekly_pull(skip: set[str] | None = None) -> dict[str, str]:
    """Run all stages; returns {stage: 'ok'|'skipped'|'FAILED: ...'}."""
    skip = skip or set()
    results: dict[str, str] = {}
    _log("=== weekly pull start ===")
    for name, run in _stages():
        if name in skip:
            results[name] = "skipped"
            _log(f"{name}: skipped")
            continue
        started = time.monotonic()
        print(f"\n--- stage: {name} ---")
        try:
            run()
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 — isolation is the whole point
            results[name] = f"FAILED: {exc}"
            _log(f"{name}: FAILED after {time.monotonic() - started:.0f}s: {exc!r}")
            traceback.print_exc()
        else:
            results[name] = "ok"
            _log(f"{name}: ok in {time.monotonic() - started:.0f}s")
    _log("=== weekly pull end ===")
    return results


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    skip: set[str] = set()
    argv = sys.argv[1:]
    if "--skip" in argv:
        idx = argv.index("--skip")
        if idx + 1 < len(argv):
            skip = {s.strip() for s in argv[idx + 1].split(",") if s.strip()}

    results = run_weekly_pull(skip)

    print("\n=== weekly pull summary ===")
    failed = 0
    for name, status in results.items():
        print(f"  {name:<18} {status}")
        failed += status.startswith("FAILED")
    print(f"Log: {LOG_DIR / 'weekly_pull.log'}")
    # Non-zero only when NOTHING ingested: partial weeks are normal (a chain
    # outage shouldn't page anyone), a totally dry run means the job is broken.
    ran = [s for s in results.values() if s != "skipped"]
    if ran and failed == len(ran):
        sys.exit(1)


if __name__ == "__main__":
    main()
