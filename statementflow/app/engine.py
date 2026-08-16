# SPDX-License-Identifier: MIT
"""The classification engine: transfer pairing + rule-based bucketing.

Implements the ordered pipeline (brief §4). Pairing is inherently cross-
transaction, so it runs as a first pass over all in-ecosystem transactions;
the remaining per-transaction steps then run in order:

  1. Paired with an own-account opposite leg (equal-and-opposite amount, dates
     within the window, different in-ecosystem accounts) -> TRANSFER. Both legs
     collapse to one edge and each records the other's account as
     ``counter_account_id``.
  2. Else a debit to a known own destination -> TRANSFER (the opposite leg was
     never uploaded; ``counter_account_id`` is the configured account).
  3. Else a credit from external -> INCOME.
  4. Else a debit -> rules -> a bucket, or UNCATEGORISED when no rule matches
     (surfaced by the review UI; treated as OTHER for display in Phase 3).

Order matters: pairing must precede rules, or an internal transfer would be
misread as income/outflow and double-count. Dedup is a *separate* step upstream
(brief §6): two accounts with equal-and-opposite amounts are a transfer pair,
not duplicates.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .models import Category
from .rules import RuleSet

DEFAULT_PAIRING_WINDOW_DAYS = 3


@dataclass(frozen=True, slots=True)
class EngineTxn:
    """The fields the engine needs about one stored transaction.

    ``posted_at`` is the full timestamp when the bank supplies a time; it
    disambiguates pairing when several same-day, same-amount candidates exist
    (real transfer legs share an exact timestamp). None when no time is known.
    """

    id: int
    account_id: str
    date: dt.date
    amount_signed: int
    description_norm: str
    posted_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """How one transaction was classified. ``category`` is a category *value*
    string: a structural one (income/transfer/other/uncategorised) or a
    configured outflow bucket id."""

    txn_id: int
    category: str
    counter_account_id: str | None = None


def _pair_transfers(
    txns: list[EngineTxn], window_days: int
) -> dict[int, str]:
    """Match equal-and-opposite legs across different accounts.

    Returns a map of txn id -> counter account id for the legs that paired.
    Greedy and deterministic: debits are considered oldest-first, and each is
    matched to the nearest *timestamp* eligible unpaired credit, then nearest
    date, then lowest id. Nearest-timestamp disambiguates when a genuine transfer
    leg and an unrelated same-day payment share an amount: a transfer's opposite
    leg carries the same second, so it wins over a payment minutes away.
    """
    window_days = abs(window_days)
    debits = sorted(
        (t for t in txns if t.amount_signed < 0),
        key=lambda t: (t.date, t.id),
    )
    credits_by_amount: dict[int, list[EngineTxn]] = {}
    for t in txns:
        if t.amount_signed > 0:
            credits_by_amount.setdefault(t.amount_signed, []).append(t)

    paired: dict[int, str] = {}
    used_credit_ids: set[int] = set()

    for debit in debits:
        candidates = [
            c
            for c in credits_by_amount.get(-debit.amount_signed, [])
            if c.id not in used_credit_ids
            and c.account_id != debit.account_id
            and abs((c.date - debit.date).days) <= window_days
        ]
        if not candidates:
            continue
        match = min(candidates, key=lambda c: _pair_sort_key(debit, c))
        used_credit_ids.add(match.id)
        paired[debit.id] = match.account_id
        paired[match.id] = debit.account_id

    return paired


def _pair_sort_key(debit: EngineTxn, credit: EngineTxn) -> tuple[int, int, int]:
    """Rank a candidate credit for a debit: nearest timestamp, then nearest date,
    then lowest id. Timestamp delta is a large sentinel when either leg lacks a
    time, so timed matches are preferred but untimed ones still pair by date."""
    if debit.posted_at is not None and credit.posted_at is not None:
        time_delta = abs(int((credit.posted_at - debit.posted_at).total_seconds()))
    else:
        time_delta = 10**12  # unknown time -> defer to date/id ordering
    return (time_delta, abs((credit.date - debit.date).days), credit.id)


def classify(
    txns: list[EngineTxn],
    *,
    ruleset: RuleSet,
    outflow_buckets: set[str] | None = None,
    income_buckets: set[str] | None = None,
    pairing_window_days: int = DEFAULT_PAIRING_WINDOW_DAYS,
) -> list[Decision]:
    """Classify every transaction, returning one Decision each (input order).

    ``outflow_buckets`` / ``income_buckets`` are the direction-specific
    assignable sets (from config); a rule only fires when its bucket is valid for
    the transaction's direction. When omitted, any rule may fire (used by pure
    unit tests that don't wire config)."""
    paired = _pair_transfers(txns, pairing_window_days)

    decisions: list[Decision] = []
    for t in txns:
        # 1. Paired own-account transfer.
        if t.id in paired:
            decisions.append(
                Decision(t.id, Category.TRANSFER.value, counter_account_id=paired[t.id])
            )
            continue

        if t.amount_signed < 0:
            # 2. Debit to a known own destination -> transfer (unpaired leg).
            dest = ruleset.known_destination_for(t.description_norm)
            if dest is not None:
                decisions.append(
                    Decision(t.id, Category.TRANSFER.value, counter_account_id=dest)
                )
                continue
            # 4. Debit -> outflow rules -> bucket, or UNCATEGORISED (needs review).
            bucket = ruleset.bucket_for(t.description_norm, outflow_buckets)
            decisions.append(Decision(t.id, bucket or Category.UNCATEGORISED.value))
        elif t.amount_signed > 0:
            # 3. Credit -> income rules -> income bucket, or UNCATEGORISED
            # (needs review, shown as generic income in the Sankey).
            bucket = ruleset.bucket_for(t.description_norm, income_buckets)
            decisions.append(Decision(t.id, bucket or Category.UNCATEGORISED.value))
        else:
            # Zero-value rows: needs review rather than fabricating a category.
            decisions.append(Decision(t.id, Category.UNCATEGORISED.value))

    return decisions
