# SPDX-License-Identifier: MIT
"""Phase 0 smoke tests: the package imports, the schema builds, the app boots.

These lock in the foundations later phases build on. All data here is synthetic;
no real statements, ever.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.db import SCHEMA_VERSION, connect, init_db
from app.main import app

_TXN_COLS = (
    "import_id, account_id, owner, date, seq, amount_signed, "
    "description_raw, description_norm, payee_norm, dedup_hash, category"
)
_TXN_PLACEHOLDERS = ",".join(["?"] * 11)


def _seed_import(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO imports(uploaded_by_user_id, uploaded_at, bank_profile, "
        "source_hash, row_count) VALUES ('alice', '2026-01-01T00:00:00Z', "
        "'monzo', 'deadbeef', 1)"
    )


def test_schema_creates_tables(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"imports", "transactions", "meta"} <= names
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert int(row["value"]) == SCHEMA_VERSION


def test_init_db_is_idempotent(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    init_db(conn)  # second call must not raise or duplicate the meta row
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert count == 1


def test_dedup_hash_is_unique(tmp_path):
    """A repeated dedup_hash is rejected -> overlapping re-uploads are idempotent."""
    conn = connect(tmp_path / "d.db")
    init_db(conn)
    _seed_import(conn)
    row = (1, "monzo-alice", "alice", "2026-01-01", 0, -1000, "COFFEE", "coffee", "coffee", "H1", "other")
    conn.execute(f"INSERT INTO transactions({_TXN_COLS}) VALUES ({_TXN_PLACEHOLDERS})", row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"INSERT INTO transactions({_TXN_COLS}) VALUES ({_TXN_PLACEHOLDERS})", row)


def test_health_endpoint():
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": __version__}
