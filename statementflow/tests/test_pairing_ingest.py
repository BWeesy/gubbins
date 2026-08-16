# SPDX-License-Identifier: MIT
"""End-to-end pairing across two accounts, exercising the parse -> store-time ->
classify path (timestamp-aware pairing).

Synthetic fixtures only. ``monzo_synthetic.csv`` has a -£200 Monzo-to-Monzo leg
at 10:00:00; ``monzo_joint_synthetic.csv`` has the matching +£200 at the same
second plus a decoy +£200 Faster payment 90s later. The decoy must not steal
the pair.
"""

from __future__ import annotations

from pathlib import Path

from app.classify import classify_stored
from app.db import connect, init_db
from app.ingest import ingest_statement
from app.models import Category
from app.profiles import load_profile
from app.rules import RuleSet

_FIXTURES = Path(__file__).parent / "fixtures"
_PROFILES = Path(__file__).resolve().parents[1] / "profiles"


def test_cross_account_pairing_prefers_same_timestamp(tmp_path):
    profile = load_profile("monzo", directory=_PROFILES)
    conn = connect(tmp_path / "sf.db")
    init_db(conn)

    ingest_statement(
        conn, profile=profile, account_id="monzo-alice", owner="alice",
        uploaded_by="alice",
        raw=(_FIXTURES / "monzo_synthetic.csv").read_bytes(),
    )
    ingest_statement(
        conn, profile=profile, account_id="monzo-joint", owner="joint",
        uploaded_by="alice",
        raw=(_FIXTURES / "monzo_joint_synthetic.csv").read_bytes(),
    )
    classify_stored(conn, ruleset=RuleSet(rules=(), known_destinations=()))

    def category(desc_like: str, amount: int, account: str) -> str:
        row = conn.execute(
            "SELECT category, counter_account_id FROM transactions "
            "WHERE amount_signed = ? AND account_id = ?",
            (amount, account),
        ).fetchone()
        return row["category"], row["counter_account_id"]

    # The -£200 debit pairs with the same-second +£200 in the joint account.
    debit_cat, debit_counter = category("transfer", -20000, "monzo-alice")
    assert debit_cat == Category.TRANSFER.value
    assert debit_counter == "monzo-joint"

    # The joint account now has one +£200 transfer (the true leg) and one +£200
    # unmatched credit (the decoy that was 90s away -> needs review).
    joint = conn.execute(
        "SELECT category FROM transactions WHERE account_id = 'monzo-joint' "
        "AND amount_signed = 20000 ORDER BY tx_time"
    ).fetchall()
    cats = [r["category"] for r in joint]
    assert cats == [Category.TRANSFER.value, Category.UNCATEGORISED.value]
