# SPDX-License-Identifier: MIT
"""Phase 4: the NatWest profile and its three new capabilities.

NatWest differs from Monzo in three ways this exercises:

  * rows are newest-first  -> profile ``reverse`` flips to chronological;
  * no transaction id but a running balance -> ``running_balance`` dedup;
  * no Name column, payee buried in the description -> ``delimited_description``
    payee extraction.

It is also the first bank with a running balance, so the §5a reconciliation
path runs on ingested data. Everything here uses a SYNTHETIC fixture.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app.classify import classify_stored
from app.config import Account, Config, User
from app.db import connect, init_db
from app.flows import flows_for_range
from app.ingest import ingest_statement
from app.parse import parse_statement
from app.profiles import load_profile
from app.rules import RuleSet
from app.strategies import PAYEE_STRATEGIES

_FIXTURE = Path(__file__).parent / "fixtures" / "natwest_synthetic.csv"
_PROFILES = Path(__file__).resolve().parents[1] / "profiles"


def _profile():
    return load_profile("natwest", directory=_PROFILES)


def _raw():
    return _FIXTURE.read_bytes()


def _config():
    return Config(
        users={"alice": User("alice", "Alice Example")},
        accounts={
            "natwest-alice": Account("natwest-alice", "NatWest", "natwest", ("alice",))
        },
    )


def _fresh_db(tmp_path):
    conn = connect(tmp_path / "nw.db")
    init_db(conn)
    return conn


def _ingest(conn):
    return ingest_statement(
        conn, profile=_profile(), account_id="natwest-alice", owner="alice",
        uploaded_by="alice", raw=_raw(),
    )


# --- profile -------------------------------------------------------------

def test_natwest_profile_loads():
    p = _profile()
    assert p.reverse is True
    assert p.dedup_strategy == "running_balance"
    assert p.payee_strategy == "delimited_description"
    assert p.balance_column == "Balance"
    assert p.id_column is None


def test_running_balance_strategy_requires_balance_column(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "name: bad\ndate_column: Date\ndate_format: '%d %b %Y'\n"
        "signed_amount_column: Value\ndedup_strategy: running_balance\n"
    )
    with pytest.raises(ValueError, match="requires: balance_column"):
        load_profile("bad", directory=tmp_path)


def test_unknown_payee_strategy_is_rejected(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "name: bad\ndate_column: Date\ndate_format: '%d %b %Y'\n"
        "signed_amount_column: Value\nid_column: X\npayee_strategy: made_up\n"
    )
    with pytest.raises(ValueError, match="payee_strategy must be one of"):
        load_profile("bad", directory=tmp_path)


# --- parsing: signs, dates, balances -------------------------------------

def test_parse_signs_dates_and_balances():
    rows = parse_statement(_raw(), _profile())
    assert len(rows) == 8
    # %d %b %Y date format parsed; signed Value; balance in pence.
    payroll = [r for r in rows if r.amount_signed == 200000][0]
    assert payroll.date == dt.date(2026, 6, 1)
    assert payroll.balance_after == 250000  # £2,500.00


# --- reverse: newest-first file -> chronological order -------------------

def test_reverse_yields_chronological_order():
    rows = parse_statement(_raw(), _profile())
    # File is newest-first; after reverse the first parsed row is the oldest.
    assert rows[0].date == dt.date(2026, 6, 1)
    assert rows[-1].date == dt.date(2026, 6, 6)


def test_reverse_gives_chronological_seq_within_a_day(tmp_path):
    conn = _fresh_db(tmp_path)
    _ingest(conn)
    # Two 03 Jun rows: chronological order is balance 2447 then 2444.
    seqs = conn.execute(
        "SELECT balance_after, seq FROM transactions WHERE date = '2026-06-03' "
        "ORDER BY seq"
    ).fetchall()
    assert [(r["balance_after"], r["seq"]) for r in seqs] == [(244700, 0), (244400, 1)]


# --- payee extraction (delimited_description) -----------------------------

@pytest.mark.parametrize(
    ("description", "expected"),
    [
        # card row: drop "1234 30JUN26 C" prefix and trailing location.
        ("1234 30JUN26 C , CORNER GARAGE , FUEL , FAKETOWN GB", "CORNER GARAGE FUEL"),
        ("1234 29JUN26 , PAYSVC , *WIDGETCO LTD , 01234567890 GB", "PAYSVC *WIDGETCO LTD"),
        ("1234 01JUN26 , BIG SHOP , LONDON GB", "BIG SHOP"),
        # non-card row: keep the first field.
        ("PARTNER TRANSFER , FP 27/06/26 30 , 99999999999999000N", "PARTNER TRANSFER"),
        ("PHONE CO LTD", "PHONE CO LTD"),
        ("ROUND UP TO 1234", "ROUND UP TO 1234"),
        ("EMPLOYER LTD , JUNE PAYROLL , FP 01/06/26 0034 , REF00000001", "EMPLOYER LTD"),
    ],
)
def test_natwest_payee_extraction(description, expected):
    assert PAYEE_STRATEGIES["delimited_description"].extract("", description) == expected


def test_payee_stored_is_the_clean_token(tmp_path):
    conn = _fresh_db(tmp_path)
    _ingest(conn)
    payees = {r["payee_norm"] for r in conn.execute("SELECT payee_norm FROM transactions")}
    assert "PAYSVC *GAMES STORE" in payees
    assert "COFFEE HUT" in payees
    assert "PHONE CO LTD" in payees
    # The card/date prefix never leaks into a payee token.
    assert not any(p.startswith("1234") for p in payees)


# --- running_balance dedup ----------------------------------------------

def test_ingest_inserts_all_rows(tmp_path):
    conn = _fresh_db(tmp_path)
    result = _ingest(conn)
    assert (result.parsed, result.inserted, result.duplicates) == (8, 8, 0)


def test_identical_same_day_rows_kept_via_balance(tmp_path):
    """Two identical £3 COFFEE HUT rows on the same day differ only by running
    balance -> running_balance dedup keeps both."""
    conn = _fresh_db(tmp_path)
    _ingest(conn)
    (coffee,) = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE amount_signed = -300"
    ).fetchone()
    assert coffee == 2


def test_reimport_is_idempotent(tmp_path):
    conn = _fresh_db(tmp_path)
    _ingest(conn)
    again = _ingest(conn)
    assert (again.inserted, again.duplicates) == (0, 8)
    (count,) = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()
    assert count == 8


# --- reconciliation (first bank with a running balance) ------------------

def test_reconciliation_runs_and_agrees(tmp_path):
    conn = _fresh_db(tmp_path)
    _ingest(conn)
    classify_stored(conn, ruleset=RuleSet(rules=(), known_destinations=()))
    node = next(
        n for n in flows_for_range(
            conn, _config(), dt.date(2026, 6, 1), dt.date(2026, 6, 30)
        )["nodes"]
        if n["id"] == "natwest-alice"
    )
    assert node["opening"] == "£500.00"      # 2500 - 2000 (first row's own amount)
    assert node["closing"] == "£2,241.60"
    assert node["reconciliation"]["ok"] is True
