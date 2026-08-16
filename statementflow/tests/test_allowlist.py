# SPDX-License-Identifier: MIT
"""The identity allowlist: when users configure an email, only those callers are
served. Synthetic data only."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import load_config
from app.main import Settings, app, get_settings

_ALICE = "alice@example.com"
_BOB = "bob@example.com"


def _write(directory, users):
    (directory / "users.yaml").write_text(yaml.safe_dump({"users": users}))
    (directory / "accounts.yaml").write_text(yaml.safe_dump({"accounts": [
        {"id": "acc", "label": "Acc", "bank": "monzo", "owners": ["alice"]},
    ]}))


def _client(tmp_path, users, *, require=True):
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    _write(cfg, users)
    settings = Settings(db_path=tmp_path / "a.db", config_dir=cfg, require_identity=require)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


_TWO = [
    {"id": "alice", "name": "Alice", "email": _ALICE},
    {"id": "bob", "name": "Bob", "email": _BOB},
]


# --- config ---------------------------------------------------------------

def test_email_is_loaded_and_matched_case_insensitively(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    _write(cfg, _TWO)
    config = load_config(cfg)
    assert config.has_login_allowlist() is True
    assert config.user_for_login("ALICE@Example.COM").id == "alice"
    assert config.user_for_login("  bob@example.com  ").id == "bob"
    assert config.user_for_login("stranger@example.com") is None
    assert config.user_for_login(None) is None


def test_no_emails_means_no_allowlist(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    _write(cfg, [{"id": "alice", "name": "Alice"}])
    assert load_config(cfg).has_login_allowlist() is False


def test_duplicate_email_is_rejected(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    _write(cfg, [
        {"id": "alice", "name": "Alice", "email": _ALICE},
        {"id": "bob", "name": "Bob", "email": _ALICE.upper()},
    ])
    with pytest.raises(ValueError, match="duplicate user email"):
        load_config(cfg)


# --- the gate -------------------------------------------------------------

def test_allowlisted_user_is_served(tmp_path):
    tc = _client(tmp_path, _TWO)
    assert tc.get("/range", headers={"Tailscale-User-Login": _ALICE}).status_code == 200


def test_other_tailnet_user_is_rejected(tmp_path):
    """The point of the feature: a valid tailnet identity that is not one of the
    household still gets a 403."""
    tc = _client(tmp_path, _TWO)
    r = tc.get("/range", headers={"Tailscale-User-Login": "stranger@example.com"})
    assert r.status_code == 403
    # The response must not disclose the identity or who is allowed.
    body = r.text.lower()
    assert "stranger" not in body and "alice" not in body


def test_missing_header_still_rejected(tmp_path):
    tc = _client(tmp_path, _TWO)
    assert tc.get("/range").status_code == 403


def test_without_emails_any_tailnet_user_is_served(tmp_path):
    """Backwards-compatible baseline: no allowlist configured -> the tailnet is
    the boundary, as documented."""
    tc = _client(tmp_path, [{"id": "alice", "name": "Alice"}])
    assert tc.get("/range", headers={"Tailscale-User-Login": "anyone@example.com"}).status_code == 200


def test_serves_before_config_is_populated(tmp_path):
    """Fresh deploy: the unit creates an empty config dir and the admin fills it
    in afterwards. The app must answer rather than 500 on every request (the
    NixOS VM test asserts exactly this)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    settings = Settings(db_path=tmp_path / "b.db", config_dir=empty, require_identity=True)
    app.dependency_overrides[get_settings] = lambda: settings
    tc = TestClient(app)
    assert tc.get("/range", headers={"Tailscale-User-Login": "anyone@example.com"}).status_code == 200
    assert tc.get("/range").status_code == 403  # header still required


def test_allowlist_applies_to_writes_too(tmp_path):
    tc = _client(tmp_path, _TWO)
    r = tc.post("/classify", headers={"Tailscale-User-Login": "stranger@example.com"})
    assert r.status_code == 403


def test_audit_records_the_configured_id_not_the_raw_login(tmp_path):
    """A recognised caller is recorded by their stable config id."""
    from pathlib import Path

    from app.db import connect

    tc = _client(tmp_path, _TWO)
    fixture = Path(__file__).parent / "fixtures" / "monzo_synthetic.csv"
    r = tc.post(
        "/upload",
        data={"account_id": "acc"},
        files={"file": ("m.csv", fixture.read_bytes(), "text/csv")},
        headers={"Tailscale-User-Login": _ALICE},
    )
    assert r.status_code == 200
    conn = connect(tmp_path / "a.db")
    uploaders = {row["uploaded_by_user_id"] for row in
                 conn.execute("SELECT uploaded_by_user_id FROM imports")}
    assert uploaders == {"alice"}  # the config id, not alice@example.com
