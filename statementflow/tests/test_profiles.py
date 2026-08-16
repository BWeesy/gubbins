# SPDX-License-Identifier: MIT
"""The debit/credit sign convention, and that every committed profile is valid.

Lloyds-family and Nationwide exports use separate positive-magnitude
debit/credit columns rather than a single signed column; this exercises that
path against a SYNTHETIC Lloyds fixture. It also loads every profile in
profiles/ so a malformed one can never ship."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db import connect, init_db
from app.ingest import ingest_statement
from app.parse import parse_statement
from app.profiles import PROFILES_DIR, load_profile

_FIXTURE = Path(__file__).parent / "fixtures" / "lloyds_synthetic.csv"


def _lloyds():
    return load_profile("lloyds", directory=PROFILES_DIR)


# --- every committed profile is valid -----------------------------------

def test_all_committed_profiles_load():
    names = sorted(p.stem for p in PROFILES_DIR.glob("*.yaml"))
    # The real two plus the camouflage set all load and validate.
    assert {
        "monzo", "natwest", "lloyds", "halifax", "bankofscotland", "tsb",
        "nationwide", "starling", "revolut", "rbs",
    } <= set(names)
    for name in names:
        profile = load_profile(name, directory=PROFILES_DIR)
        assert profile.name  # loads + validates (strategies, sign convention, ...)


# --- debit/credit sign convention ---------------------------------------

def test_debit_credit_columns_become_signed_pence():
    rows = parse_statement(_FIXTURE.read_bytes(), _lloyds())
    assert len(rows) == 4
    # reverse: true -> chronological; the first row is the oldest (payroll).
    assert rows[0].amount_signed == 200000    # Credit Amount 2000.00 -> +
    assert rows[0].balance_after == 250000
    debits = sorted(r.amount_signed for r in rows if r.amount_signed < 0)
    assert debits == [-5000, -300, -300]       # BIG SHOP + two COFFEE HUT


def test_debit_credit_running_balance_dedup(tmp_path):
    conn = connect(tmp_path / "l.db")
    init_db(conn)
    result = ingest_statement(
        conn, profile=_lloyds(), account_id="lloyds-1", owner="alice",
        uploaded_by="alice", raw=_FIXTURE.read_bytes(),
    )
    assert (result.parsed, result.inserted) == (4, 4)
    # The two identical £3 COFFEE HUT rows differ only by running balance -> both kept.
    (coffee,) = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE amount_signed = -300"
    ).fetchone()
    assert coffee == 2
    # Re-import is idempotent.
    again = ingest_statement(
        conn, profile=_lloyds(), account_id="lloyds-1", owner="alice",
        uploaded_by="alice", raw=_FIXTURE.read_bytes(),
    )
    assert (again.inserted, again.duplicates) == (0, 4)


# --- sign-convention validation -----------------------------------------

def test_sign_convention_must_be_exactly_one(tmp_path):
    base = "name: bad\ndate_column: D\ndate_format: '%d/%m/%Y'\nid_column: X\n"
    # Neither convention.
    (tmp_path / "bad.yaml").write_text(base)
    with pytest.raises(ValueError, match="exactly one sign convention"):
        load_profile("bad", directory=tmp_path)
    # Both conventions.
    (tmp_path / "bad2.yaml").write_text(
        base.replace("name: bad", "name: bad2")
        + "signed_amount_column: A\ndebit_column: DB\ncredit_column: CR\n"
    )
    with pytest.raises(ValueError, match="exactly one sign convention"):
        load_profile("bad2", directory=tmp_path)
