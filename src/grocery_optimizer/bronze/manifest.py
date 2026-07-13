"""Bronze artifact writer.

Every raw fetch is preserved verbatim on disk BEFORE any parsing (the time
machine — weekly ads expire and are unrecoverable), and a row is recorded in
``bronze_manifest`` pointing at the file. Never parse-and-discard.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .. import config


def _extension_for(content_type: str | None) -> str:
    if content_type:
        ct = content_type.lower()
        if "json" in ct:
            return "json"
        if "html" in ct:
            return "html"
        if "xml" in ct:
            return "xml"
    return "bin"


def save_artifact(
    con: duckdb.DuckDBPyConnection,
    *,
    source: str,
    source_kind: str,
    content: bytes | str,
    fetched_at: datetime | None = None,
    request_url: str | None = None,
    request_params: dict | None = None,
    http_status: int | None = None,
    content_type: str | None = None,
    region: str | None = None,
    store_id: str | None = None,
    ad_week=None,
    parse_status: str = "pending",
    notes: str | None = None,
) -> tuple[int, Path]:
    """Persist a raw artifact to ``data/bronze/`` and record it in the manifest.

    Returns ``(manifest_id, absolute_path)``. The manifest stores the path
    relative to ``data/bronze/`` with forward slashes so it is portable.
    """
    config.ensure_dirs()
    content_bytes = content.encode("utf-8") if isinstance(content, str) else content
    fetched_at = fetched_at or datetime.now(timezone.utc)

    sha = hashlib.sha256(content_bytes).hexdigest()
    ext = _extension_for(content_type)
    day = fetched_at.strftime("%Y-%m-%d")
    stamp = fetched_at.strftime("%Y%m%dT%H%M%S%f")
    rel_path = Path(source) / day / f"{stamp}_{sha[:8]}.{ext}"
    abs_path = config.BRONZE_DIR / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(content_bytes)

    params_json = json.dumps(request_params) if request_params is not None else None
    manifest_id = con.execute(
        """
        INSERT INTO bronze_manifest (
            source, source_kind, fetched_at, ad_week, region, store_id,
            request_url, request_params, http_status, content_type,
            raw_path, content_sha256, byte_size, parse_status, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING manifest_id
        """,
        [
            source,
            source_kind,
            fetched_at,
            ad_week,
            region,
            store_id,
            request_url,
            params_json,
            http_status,
            content_type,
            rel_path.as_posix(),
            sha,
            len(content_bytes),
            parse_status,
            notes,
        ],
    ).fetchone()[0]

    return manifest_id, abs_path
