# SPDX-License-Identifier: MIT
"""Raw statement retention (brief §10): keep every ingested file so an import
can be re-run after a profile or parser fix. Synthetic fixtures only."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db import connect, init_db
from app.ingest import ingest_statement, retain_raw
from app.profiles import PROFILES_DIR, load_profile

_FIXTURE = Path(__file__).parent / "fixtures" / "monzo_synthetic.csv"


def _profile():
    return load_profile("monzo", directory=PROFILES_DIR)


def _db(tmp_path):
    conn = connect(tmp_path / "r.db")
    init_db(conn)
    return conn


def test_upload_is_retained(tmp_path):
    raw_dir = tmp_path / "raw"
    result = ingest_statement(
        _db(tmp_path), profile=_profile(), account_id="monzo-alice", owner="alice",
        uploaded_by="alice", raw=_FIXTURE.read_bytes(), raw_dir=raw_dir,
    )
    kept = list(raw_dir.rglob("*.csv"))
    assert len(kept) == 1
    assert kept[0].read_bytes() == _FIXTURE.read_bytes()  # byte-identical
    assert result.retained_as == str(kept[0])
    assert kept[0].parent.name == "monzo-alice"  # filed per account


def test_same_file_retained_once(tmp_path):
    """Content-hash naming: re-uploading the same export does not pile up copies."""
    raw_dir = tmp_path / "raw"
    conn = _db(tmp_path)
    for _ in range(3):
        ingest_statement(
            conn, profile=_profile(), account_id="monzo-alice", owner="alice",
            uploaded_by="alice", raw=_FIXTURE.read_bytes(), raw_dir=raw_dir,
        )
    assert len(list(raw_dir.rglob("*.csv"))) == 1


def test_different_files_both_retained(tmp_path):
    raw_dir = tmp_path / "raw"
    conn = _db(tmp_path)
    other = (Path(__file__).parent / "fixtures" / "monzo_joint_synthetic.csv").read_bytes()
    ingest_statement(conn, profile=_profile(), account_id="a", owner="x",
                     uploaded_by="x", raw=_FIXTURE.read_bytes(), raw_dir=raw_dir)
    ingest_statement(conn, profile=_profile(), account_id="a", owner="x",
                     uploaded_by="x", raw=other, raw_dir=raw_dir)
    assert len(list(raw_dir.rglob("*.csv"))) == 2


def test_unparseable_upload_is_still_retained(tmp_path):
    """The file that failed to parse is exactly the one a profile fix rescues,
    so retention happens before parsing."""
    raw_dir = tmp_path / "raw"
    with pytest.raises((ValueError, KeyError)):
        ingest_statement(
            _db(tmp_path), profile=_profile(), account_id="monzo-alice", owner="alice",
            uploaded_by="alice", raw=b"not,a,statement\n1,2,3\n", raw_dir=raw_dir,
        )
    assert len(list(raw_dir.rglob("*.csv"))) == 1


def test_retention_is_opt_in(tmp_path):
    """No raw_dir -> nothing written (keeps unit tests and dev runs clean)."""
    result = ingest_statement(
        _db(tmp_path), profile=_profile(), account_id="monzo-alice", owner="alice",
        uploaded_by="alice", raw=_FIXTURE.read_bytes(),
    )
    assert result.retained_as is None
    assert not (tmp_path / "raw").exists()


def test_account_id_cannot_escape_the_raw_dir(tmp_path):
    """A path separator in an account id must not write outside raw_dir."""
    raw_dir = tmp_path / "raw"
    dest = retain_raw(raw_dir, "../../escape", b"data")
    assert raw_dir in dest.parents
    assert not (tmp_path.parent / "escape").exists()
