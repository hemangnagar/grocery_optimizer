"""OpenLineage emission for pipeline runs.

Emit, never depend: every normalize run writes a spec-conformant OpenLineage
RunEvent to ``data/lineage/`` unconditionally, and additionally POSTs it to a
collector only when ``OPENLINEAGE_URL`` is set (e.g. Marquez at
``http://host:5000``; ``OPENLINEAGE_API_KEY`` optional). No collector, no
extra dependency, no failure mode — a lineage POST that fails never fails the
pipeline run it describes.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import duckdb

from .. import config

LINEAGE_DIR = config.DATA_DIR / "lineage"

_PRODUCER = "https://github.com/hemangnagar/grocery_optimizer"
_SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/definitions/RunEvent"
_SCHEMA_FACET = "https://openlineage.io/spec/facets/1-1-1/SchemaDatasetFacet.json"

JOB_NAMESPACE = "grocery_optimizer"


def _schema_facet(con: duckdb.DuckDBPyConnection, relation: str) -> dict:
    fields = [
        {"name": name, "type": col_type}
        for name, col_type, *_ in con.execute(f"DESCRIBE {relation}").fetchall()
    ]
    return {
        "_producer": _PRODUCER,
        "_schemaURL": _SCHEMA_FACET,
        "fields": fields,
    }


def build_run_event(
    con: duckdb.DuckDBPyConnection,
    job_name: str,
    counts: dict | None = None,
) -> dict:
    """A COMPLETE RunEvent: bronze manifests in, gold views out."""
    sources = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT source FROM bronze_manifest ORDER BY source"
        ).fetchall()
    ]
    event = {
        "eventType": "COMPLETE",
        "eventTime": datetime.now(timezone.utc).isoformat(),
        "producer": _PRODUCER,
        "schemaURL": _SCHEMA_URL,
        "run": {"runId": str(uuid.uuid4())},
        "job": {"namespace": JOB_NAMESPACE, "name": job_name},
        "inputs": [
            {"namespace": f"{JOB_NAMESPACE}.bronze", "name": source}
            for source in sources
        ],
        "outputs": [
            {
                "namespace": f"{JOB_NAMESPACE}.gold",
                "name": relation,
                "facets": {"schema": _schema_facet(con, relation)},
            }
            for relation in ("gold_current_prices", "gold_cheapest_source_per_item")
        ],
    }
    if counts:
        event["run"]["facets"] = {
            "processing_counts": {
                "_producer": _PRODUCER,
                "_schemaURL": _SCHEMA_URL,
                **{k: v for k, v in counts.items() if isinstance(v, int)},
            }
        }
    return event


def emit_lineage(
    con: duckdb.DuckDBPyConnection, job_name: str, counts: dict | None = None
) -> tuple[dict, str | None]:
    """Write the RunEvent to data/lineage/ (always) and POST it (if configured).

    Returns (event, delivery) where delivery is 'posted', an error note, or
    None when no collector is configured.
    """
    event = build_run_event(con, job_name, counts)
    LINEAGE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = LINEAGE_DIR / f"{job_name}_{stamp}_{event['run']['runId'][:8]}.json"
    path.write_text(json.dumps(event, indent=2), encoding="utf-8")

    url = os.environ.get("OPENLINEAGE_URL")
    if not url:
        return event, None
    try:
        import httpx

        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get("OPENLINEAGE_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = httpx.post(
            url.rstrip("/") + "/api/v1/lineage",
            json=event,
            headers=headers,
            timeout=10.0,
        )
        return event, "posted" if resp.status_code < 300 else f"HTTP {resp.status_code}"
    except Exception as exc:  # lineage delivery never fails the run it describes
        return event, f"post failed: {exc}"
