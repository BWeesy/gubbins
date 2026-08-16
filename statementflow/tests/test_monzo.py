# SPDX-License-Identifier: MIT
"""Phase 1: the Monzo profile, parser and idempotent import.

Everything here runs against a SYNTHETIC fixture (tests/fixtures/monzo_synthetic.csv)
that mirrors Monzo's column layout but contains no real data. The fixture
deliberately includes the tricky cases:

  * two genuine same-day, same-amount, same-payee purchases (must stay distinct);
  * a foreign-currency purchase (signed Amount is GBP, local is EUR);
  * a positive "card payment" refund;
  * a peer transfer with a blank Description (payee name is the fallback).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from app.db import connect, init_db
from app.ingest import ingest_statement
from app.models import Category
from app.profiles import CsvProfile, load_profile

_FIXTURE = Path(__file__).parent / "fixtures" / "monzo_synthetic.csv"
_PROFILES = Path(__file__).resolve().parents[1] / "profiles"


def _profile() -> CsvProfile:
    return load_profile("monzo", directory=_PROFILES)


def _raw() -> bytes:
    return _FIXTURE.read_bytes()


# --- profile -------------------------------------------------------------

def test_monzo_profile_loads():
    p = _profile()
    assert p.name == "monzo"
    assert p.signed_amount_column == "Amount"
    assert p.dedup_strategy == "source_id"
    assert p.id_column == "Transaction ID"
    assert p.balance_column is None  # Monzo CSV has no running balance


# --- parsing -------------------------------------------------------------

def test_parse_row_count_and_signs():
    from app.parse import parse_statement

    rows = parse_statement(_raw(), _profile())
    assert len(rows) == 6
    by_amount = sorted(r.amount_signed for r in rows)
    # two -350 (coffee x2), -20000 (transfer out), -430 (foreign), +200 (refund),
    # +150000 (salary)
    assert by_amount == [-20000, -430, -350, -350, 200, 150000]


def test_parse_foreign_currency_uses_gbp_amount():
    from app.parse import parse_statement

    rows = parse_statement(_raw(), _profile())
    foreign = [r for r in rows if "FOREIGN CAFE" in r.description_norm]
    assert len(foreign) == 1
    assert foreign[0].amount_signed == -430  # the GBP Amount, not the EUR -5.00


def test_parse_blank_description_falls_back_to_name():
    from app.parse import parse_statement

    rows = parse_statement(_raw(), _profile())
    transfer = [r for r in rows if r.amount_signed == -20000][0]
    assert transfer.description_raw == "Joint Account"


def test_parse_captures_category_hint():
    from app.parse import parse_statement

    rows = parse_statement(_raw(), _profile())
    salary = [r for r in rows if r.amount_signed == 150000][0]
    assert salary.category_hint == "Income"


# --- ingest + dedup ------------------------------------------------------

def _fresh_db(tmp_path):
    conn = connect(tmp_path / "sf.db")
    init_db(conn)
    return conn


def test_ingest_inserts_all_rows(tmp_path):
    conn = _fresh_db(tmp_path)
    result = ingest_statement(
        conn, profile=_profile(), account_id="monzo-alice", owner="alice",
        uploaded_by="alice", raw=_raw(),
    )
    assert (result.parsed, result.inserted, result.duplicates) == (6, 6, 0)
    (count,) = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()
    assert count == 6
    # Everything lands UNCATEGORISED; classification is Phase 2.
    cats = {r["category"] for r in conn.execute("SELECT category FROM transactions")}
    assert cats == {Category.UNCATEGORISED.value}


def test_identical_same_day_rows_are_both_kept(tmp_path):
    """The two synthetic COFFEE BAR rows share date, amount and description but
    have distinct Monzo ids -> source_id dedup keeps both."""
    conn = _fresh_db(tmp_path)
    ingest_statement(
        conn, profile=_profile(), account_id="monzo-alice", owner="alice",
        uploaded_by="alice", raw=_raw(),
    )
    (coffee,) = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE amount_signed = -350"
    ).fetchone()
    assert coffee == 2


def test_reimport_is_idempotent(tmp_path):
    conn = _fresh_db(tmp_path)
    ingest_statement(
        conn, profile=_profile(), account_id="monzo-alice", owner="alice",
        uploaded_by="alice", raw=_raw(),
    )
    again = ingest_statement(
        conn, profile=_profile(), account_id="monzo-alice", owner="alice",
        uploaded_by="alice", raw=_raw(),
    )
    assert (again.inserted, again.duplicates) == (0, 6)
    (count,) = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()
    assert count == 6  # no growth on re-upload


def test_same_txn_in_different_account_is_not_a_duplicate(tmp_path):
    """Dedup keys include the account id, so the same Monzo export imported under
    two different accounts does not cross-dedupe."""
    conn = _fresh_db(tmp_path)
    ingest_statement(
        conn, profile=_profile(), account_id="monzo-alice", owner="alice",
        uploaded_by="alice", raw=_raw(),
    )
    result = ingest_statement(
        conn, profile=_profile(), account_id="monzo-bob", owner="bob",
        uploaded_by="bob", raw=_raw(),
    )
    assert result.inserted == 6


def test_seq_orders_same_day_rows_by_time(tmp_path):
    conn = _fresh_db(tmp_path)
    ingest_statement(
        conn, profile=_profile(), account_id="monzo-alice", owner="alice",
        uploaded_by="alice", raw=_raw(),
    )
    # 2026-05-03 has the foreign purchase (08:00) then the refund (18:00).
    rows = conn.execute(
        "SELECT amount_signed, seq FROM transactions WHERE date = '2026-05-03' "
        "ORDER BY seq"
    ).fetchall()
    assert [(r["amount_signed"], r["seq"]) for r in rows] == [(-430, 0), (200, 1)]


def test_unknown_dedup_strategy_is_rejected():
    """An unsupported strategy reaching the hasher raises rather than silently
    keying on nothing."""
    import pytest

    from app.parse import parse_statement
    from app.strategies import dedup_hash

    profile = dataclasses.replace(_profile(), dedup_strategy="made_up")
    row = parse_statement(_raw(), _profile())[0]
    with pytest.raises(ValueError, match="unsupported dedup strategy"):
        dedup_hash(profile, "monzo-alice", row)
