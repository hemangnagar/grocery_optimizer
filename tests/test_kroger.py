"""No-network tests for the Kroger client and the bronze manifest writer."""

from __future__ import annotations

import duckdb
import pytest

from grocery_optimizer import config
from grocery_optimizer.bronze.kroger import KrogerAuthError, KrogerClient
from grocery_optimizer.bronze.manifest import save_artifact
from grocery_optimizer.db import init_db


def test_client_requires_credentials():
    with pytest.raises(KrogerAuthError):
        KrogerClient(None, None)
    with pytest.raises(KrogerAuthError):
        KrogerClient("id", "")


def test_client_builds_location_params():
    # Constructing with dummy creds must not hit the network.
    client = KrogerClient("dummy-id", "dummy-secret")
    try:
        assert client.base_url == "https://api.kroger.com/v1"
        assert client.scope == "product.compact"
    finally:
        client.close()


def test_save_artifact_writes_file_and_manifest(tmp_path, monkeypatch):
    # Redirect bronze output into a temp dir so the test leaves no real files.
    monkeypatch.setattr(config, "BRONZE_DIR", tmp_path / "bronze")

    con = duckdb.connect(":memory:")
    init_db(con)

    body = b'{"data": [{"chain": "HARRISTEETER"}]}'
    manifest_id, abs_path = save_artifact(
        con,
        source="kroger",
        source_kind="api",
        content=body,
        request_url="https://api.kroger.com/v1/locations?filter.zipCode.near=20009",
        request_params={"zip": "20009"},
        http_status=200,
        content_type="application/json; charset=utf-8",
        region="zip:20009",
        notes="test",
    )

    # File written verbatim with a .json extension.
    assert abs_path.exists()
    assert abs_path.read_bytes() == body
    assert abs_path.suffix == ".json"

    row = con.execute(
        """
        SELECT source, source_kind, http_status, byte_size, content_sha256,
               raw_path, parse_status
        FROM bronze_manifest WHERE manifest_id = ?
        """,
        [manifest_id],
    ).fetchone()
    source, kind, status, size, sha, raw_path, parse_status = row
    assert source == "kroger"
    assert kind == "api"
    assert status == 200
    assert size == len(body)
    assert len(sha) == 64  # sha256 hex
    assert raw_path.startswith("kroger/")  # relative, forward slashes
    assert parse_status == "pending"
