# SPDX-License-Identifier: MIT
"""View features on top of /flows: net-position summary, transfers filter,
split-unreviewed, per-owner restriction, and transaction drill-down."""

from __future__ import annotations

import datetime as dt

import pytest

from app.config import Account, CategoryDef, Config, User
from app.db import connect, init_db
from app.flows import flows_for_range, transactions_for

MAY = (dt.date(2026, 5, 1), dt.date(2026, 5, 31))
_seq = [0]


def _config():
    return Config(
        users={"a": User("a", "Alice"), "b": User("b", "Bob")},
        accounts={
            "alice": Account("alice", "Alice Monzo", "monzo", ("a",)),
            "bob": Account("bob", "Bob Monzo", "monzo", ("b",)),
            "joint": Account("joint", "Joint", "monzo", ("a", "b")),
        },
        categories={"energy": CategoryDef("energy", "Energy", "outflow"),
                    "salary": CategoryDef("salary", "Salary", "income")},
    )


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "v.db")
    init_db(c)
    c.execute("INSERT INTO imports(uploaded_by_user_id, uploaded_at, bank_profile, "
              "source_hash, row_count) VALUES ('a','t','monzo','h',1)")
    return c


def _add(conn, account, amount, category, counter=None, desc="d"):
    _seq[0] += 1
    conn.execute(
        "INSERT INTO transactions(import_id, account_id, owner, date, seq, amount_signed, "
        "description_raw, description_norm, payee_norm, dedup_hash, category, counter_account_id) "
        "VALUES (1, ?, 'x', '2026-05-10', ?, ?, ?, 'd', 'd', ?, ?, ?)",
        (account, _seq[0], amount, desc, f"h{_seq[0]}", category, counter))


def _scenario(conn):
    _add(conn, "alice", 300000, "salary")
    _add(conn, "alice", -8000, "energy")
    _add(conn, "alice", -20000, "transfer", counter="joint")   # alice -> joint
    _add(conn, "joint", 20000, "transfer", counter="alice")    # joint <- alice
    _add(conn, "joint", -15000, "energy")
    _add(conn, "bob", 100000, "uncategorised")                 # unmatched credit
    _add(conn, "bob", -3000, "uncategorised")                  # unmatched debit
    conn.commit()


# --- summary -------------------------------------------------------------

def test_summary_net_position_per_owner(conn):
    _scenario(conn)
    summary = flows_for_range(conn, _config(), *MAY)["summary"]
    alice = summary["by_owner"]["a"]
    assert alice["income"] == 300000
    assert alice["outflow"] == 8000
    assert alice["transfers_out"] == 20000
    assert alice["net"] == 300000 - 8000 - 20000
    joint = summary["by_owner"]["joint"]
    assert joint["transfers_in"] == 20000
    assert joint["net"] == 20000 - 15000
    # by_account carries a label + owner for the tiles.
    assert summary["by_account"]["alice"]["owner"] == "a"


# --- transfers filter ----------------------------------------------------

def test_transfers_exclude_drops_transfer_edges(conn):
    _scenario(conn)
    flows = flows_for_range(conn, _config(), *MAY, transfers="exclude")
    assert not any(e for e in flows["edges"] if e["source"] == "alice" and e["target"] == "joint")
    assert any(e for e in flows["edges"] if e["target"] == "energy")  # sinks remain


def test_transfers_only_keeps_only_transfers(conn):
    _scenario(conn)
    flows = flows_for_range(conn, _config(), *MAY, transfers="only")
    kinds = {(e["source"], e["target"]) for e in flows["edges"]}
    assert kinds == {("alice", "joint")}
    assert not any(n for n in flows["nodes"] if n["type"] == "sink")


# --- split unreviewed ----------------------------------------------------

def test_split_unreviewed_makes_its_own_nodes(conn):
    _scenario(conn)
    merged = flows_for_range(conn, _config(), *MAY)
    assert not any(n["id"] == "uncategorised" for n in merged["nodes"])  # folded in

    split = flows_for_range(conn, _config(), *MAY, split_unreviewed=True)
    ids = {n["id"] for n in split["nodes"]}
    assert "uncategorised" in ids  # both the credit (income) and debit (sink) sides


# --- per-owner restriction (lanes) --------------------------------------

def test_account_ids_restricts_to_one_owner(conn):
    _scenario(conn)
    flows = flows_for_range(conn, _config(), *MAY, account_ids={"bob"})
    account_nodes = {n["id"] for n in flows["nodes"] if n["type"] == "account"}
    assert account_nodes == {"bob"}


# --- drill-down ----------------------------------------------------------

def test_transactions_for_a_category(conn):
    _scenario(conn)
    rows = transactions_for(conn, _config(), *MAY, category="energy")
    assert {r["amount_pence"] for r in rows} == {-8000, -15000}


def test_transactions_for_generic_other_folds_in_uncategorised_debits(conn):
    _scenario(conn)
    rows = transactions_for(conn, _config(), *MAY, category="other")
    # No reviewed 'other' rows, but the uncategorised debit folds in.
    assert [r["amount_pence"] for r in rows] == [-3000]


def test_transactions_owner_filter(conn):
    _scenario(conn)
    rows = transactions_for(conn, _config(), *MAY, category="salary", account_ids={"bob"})
    assert rows == []  # salary is alice's, not bob's