# SPDX-License-Identifier: MIT
"""Phase 3: the /flows month payload -- nodes, edges, net delta, reconciliation.

Data is built directly in the DB so a single test can assert exact pence. Two
accounts exercise transfers; balances are set on one account so the §5a
reconciliation path runs (Monzo has no balances, so it never would otherwise).
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.config import Account, Config, User
from app.db import connect, init_db
from app.flows import flows_for_range

_next = [0]

# All fixtures land in May 2026; this inclusive range spans the whole month.
_MAY = (dt.date(2026, 5, 1), dt.date(2026, 5, 31))


def _insert(conn, *, account, amount, category, date="2026-05-10",
            counter=None, balance=None, tx_time=None):
    _next[0] += 1
    conn.execute(
        "INSERT INTO imports(uploaded_by_user_id, uploaded_at, bank_profile, "
        "source_hash, row_count) VALUES ('u','t','monzo','h',1)"
    )
    conn.execute(
        "INSERT INTO transactions(import_id, account_id, owner, date, tx_time, seq, "
        "amount_signed, balance_after, description_raw, description_norm, payee_norm, "
        "dedup_hash, category, counter_account_id, reviewed) "
        "VALUES (1, ?, 'x', ?, ?, ?, ?, ?, 'd', 'd', 'd', ?, ?, ?, 0)",
        (account, date, tx_time, _next[0], amount, balance, f"h{_next[0]}",
         category, counter),
    )


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "f.db")
    init_db(c)
    return c


def _config():
    users = {"a": User("a", "Alice")}
    accounts = {
        "monzo": Account("monzo", "Monzo", "monzo", ("a",)),
        "joint": Account("joint", "Joint", "natwest", ("a",)),
    }
    return Config(users=users, accounts=accounts)


def test_income_transfer_and_sink_edges(conn):
    _insert(conn, account="monzo", amount=150000, category="income")
    _insert(conn, account="monzo", amount=-20000, category="transfer", counter="joint")
    _insert(conn, account="joint", amount=20000, category="transfer", counter="monzo")
    _insert(conn, account="monzo", amount=-5000, category="bills")
    _insert(conn, account="monzo", amount=-3000, category="uncategorised")  # -> other
    conn.commit()

    flows = flows_for_range(conn, _config(), *_MAY)
    edges = {(e["source"], e["target"]): e["value"] for e in flows["edges"]}

    assert edges[("income", "monzo")] == 1500.0
    assert edges[("monzo", "joint")] == 200.0     # one collapsed transfer edge
    assert ("joint", "monzo") not in edges         # credit leg not double-counted
    assert edges[("monzo", "bills")] == 50.0
    assert edges[("monzo", "other")] == 30.0       # uncategorised shown as other


def test_net_delta_from_flow_sum(conn):
    _insert(conn, account="monzo", amount=150000, category="income")
    _insert(conn, account="monzo", amount=-20000, category="transfer", counter="joint")
    _insert(conn, account="monzo", amount=-5000, category="bills")
    conn.commit()

    flows = flows_for_range(conn, _config(), *_MAY)
    monzo = next(n for n in flows["nodes"] if n["id"] == "monzo")
    assert monzo["net_delta_pence"] == 125000  # 1500 - 200 - 50
    assert monzo["net_delta"] == "£1,250.00"
    # No balances on these rows -> no reconciliation block.
    assert "reconciliation" not in monzo


def test_reconciliation_ok_when_balances_agree(conn):
    # Running balance after each row; opening = first.balance - first.amount.
    _insert(conn, account="joint", amount=10000, category="income",
            balance=110000, date="2026-05-02")
    _insert(conn, account="joint", amount=-4000, category="bills",
            balance=106000, date="2026-05-05")
    conn.commit()

    node = next(n for n in flows_for_range(conn, _config(), *_MAY)["nodes"]
                if n["id"] == "joint")
    assert node["opening"] == "£1,000.00"    # 110000 - 10000
    assert node["closing"] == "£1,060.00"
    assert node["reconciliation"]["ok"] is True


def test_reconciliation_flags_missing_transaction(conn):
    # Balances jump by more than the flows account for -> a row is missing.
    _insert(conn, account="joint", amount=10000, category="income",
            balance=110000, date="2026-05-02")
    _insert(conn, account="joint", amount=-4000, category="bills",
            balance=200000, date="2026-05-05")  # balance implies +£940 unexplained
    conn.commit()

    node = next(n for n in flows_for_range(conn, _config(), *_MAY)["nodes"]
                if n["id"] == "joint")
    assert node["reconciliation"]["ok"] is False


def test_month_filtering(conn):
    _insert(conn, account="monzo", amount=-5000, category="bills", date="2026-05-31")
    _insert(conn, account="monzo", amount=-7000, category="bills", date="2026-06-01")
    conn.commit()

    may = flows_for_range(conn, _config(), *_MAY)
    assert {(e["source"], e["target"]): e["value"] for e in may["edges"]} == {("monzo", "bills"): 50.0}


def test_owner_and_account_attribution_on_nodes_and_edges(conn):
    # 'monzo' is sole-owned by alice; 'joint' is co-owned -> owner 'joint'.
    cfg = Config(
        users={"a": User("a", "Alice"), "b": User("b", "Bob")},
        accounts={
            "monzo": Account("monzo", "Monzo", "monzo", ("a",)),
            "joint": Account("joint", "Joint", "natwest", ("a", "b")),
        },
    )
    _insert(conn, account="monzo", amount=150000, category="income")
    _insert(conn, account="monzo", amount=-5000, category="bills")
    _insert(conn, account="joint", amount=-9000, category="bills")
    conn.commit()

    flows = flows_for_range(conn, cfg, *_MAY)
    owners = {n["id"]: n["owner"] for n in flows["nodes"] if n["type"] == "account"}
    assert owners == {"monzo": "a", "joint": "joint"}
    # Each edge is attributed to the account it touches.
    attribution = {(e["source"], e["target"]): e["account"] for e in flows["edges"]}
    assert attribution[("income", "monzo")] == "monzo"   # income -> its target account
    assert attribution[("monzo", "bills")] == "monzo"    # outflow -> its source account
    assert attribution[("joint", "bills")] == "joint"


def test_arbitrary_range_spans_month_boundary(conn):
    _insert(conn, account="monzo", amount=-5000, category="bills", date="2026-05-31")
    _insert(conn, account="monzo", amount=-7000, category="bills", date="2026-06-01")
    conn.commit()

    # An inclusive range across the month boundary picks up both rows.
    flows = flows_for_range(conn, _config(), dt.date(2026, 5, 31), dt.date(2026, 6, 1))
    assert flows["start"] == "2026-05-31"
    assert flows["end"] == "2026-06-01"
    assert {(e["source"], e["target"]): e["value"] for e in flows["edges"]} == {
        ("monzo", "bills"): 120.0
    }
