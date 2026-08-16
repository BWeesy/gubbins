# SPDX-License-Identifier: MIT
"""Run the classification engine over the stored transactions.

Bridges the pure engine (``engine.py``) to the DB: load every transaction (all
of them, because pairing is cross-transaction), classify, then write decisions
back -- but never onto a row a human has already reviewed. A re-run is therefore
idempotent for confident rows and respects manual verdicts.
"""

from __future__ import annotations

import datetime as dt
from sqlite3 import Connection

from .db import apply_classification, reviewed_ids
from .engine import DEFAULT_PAIRING_WINDOW_DAYS, EngineTxn, classify
from .rules import RuleSet


def _posted_at(date: str, tx_time: str | None) -> dt.datetime | None:
    """Combine the stored ISO date and HH:MM:SS into a timestamp, or None when no
    time was recorded (so untimed banks fall back to date-only pairing)."""
    if not tx_time:
        return None
    try:
        return dt.datetime.combine(
            dt.date.fromisoformat(date), dt.time.fromisoformat(tx_time)
        )
    except ValueError:
        return None


def classify_stored(
    conn: Connection,
    *,
    ruleset: RuleSet,
    outflow_buckets: set[str] | None = None,
    income_buckets: set[str] | None = None,
    pairing_window_days: int = DEFAULT_PAIRING_WINDOW_DAYS,
) -> int:
    """Classify all stored transactions; return how many rows were updated.

    Reviewed rows still take part in pairing (a manually-confirmed leg can be a
    counter to another), but their stored category is never overwritten.
    """
    rows = conn.execute(
        "SELECT id, account_id, date, tx_time, amount_signed, description_norm "
        "FROM transactions"
    ).fetchall()
    txns = [
        EngineTxn(
            id=row["id"],
            account_id=row["account_id"],
            date=dt.date.fromisoformat(row["date"]),
            amount_signed=row["amount_signed"],
            description_norm=row["description_norm"],
            posted_at=_posted_at(row["date"], row["tx_time"]),
        )
        for row in rows
    ]

    protected = reviewed_ids(conn)
    decisions = classify(
        txns, ruleset=ruleset, outflow_buckets=outflow_buckets,
        income_buckets=income_buckets, pairing_window_days=pairing_window_days,
    )

    updated = 0
    for decision in decisions:
        if decision.txn_id in protected:
            continue
        apply_classification(
            conn,
            txn_id=decision.txn_id,
            category=decision.category,
            counter_account_id=decision.counter_account_id,
        )
        updated += 1
    conn.commit()
    return updated
