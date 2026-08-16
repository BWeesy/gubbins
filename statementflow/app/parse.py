# SPDX-License-Identifier: MIT
"""Parsing: a bank export + its profile -> normalised statement rows.

Applies a public bank profile (see ``profiles.py``) to a raw CSV export and
yields ``StatementRow`` values: bank-agnostic, signed integer pence. All the
per-bank sign/column/date quirks are absorbed here so everything downstream sees
one uniform shape.

A ``StatementRow`` carries only what the file itself supplies. Account, owner and
``seq`` are attached later at ingest time (owner from account config, never the
uploader; seq from same-day ordering).
"""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass

from .money import to_pence
from .profiles import CsvProfile
from .strategies import payee_for
from .text import normalise


@dataclass(frozen=True, slots=True)
class StatementRow:
    """One parsed statement line, before it is tied to an account."""

    date: dt.date
    amount_signed: int  # minor units (pence); credit +, debit -
    description_raw: str
    description_norm: str
    # The merchant/payee token, normalised. This is what "remember this payee"
    # learns on -- NOT the full description, which carries location/terminal
    # noise and so would only ever re-match the identical transaction. Derived
    # per the profile's payee_strategy.
    payee_norm: str
    time: str  # HH:MM:SS or "" -- used only to order same-day rows
    source_id: str  # the bank's stable txn id, or "" if the profile maps none
    category_hint: str | None  # the bank's own category, verbatim
    balance_after: int | None  # running balance after this row (pence), or None


def _signed_amount(profile: CsvProfile, row: dict) -> int:
    """The row's amount in signed pence, from whichever sign convention the
    profile declares: a single signed column, or a positive-magnitude
    debit/credit pair (credit +, debit -)."""
    if profile.signed_amount_column:
        return to_pence(row[profile.signed_amount_column])
    debit = (row.get(profile.debit_column) or "").strip()
    credit = (row.get(profile.credit_column) or "").strip()
    return (to_pence(credit) if credit else 0) - (to_pence(debit) if debit else 0)


def parse_statement(raw: bytes, profile: CsvProfile) -> list[StatementRow]:
    """Parse a raw CSV export into StatementRows using ``profile``.

    When the profile marks the export ``reverse`` (newest-first, e.g. NatWest),
    rows are flipped to chronological order so ``seq`` and opening/closing come
    out right downstream."""
    rows = list(_iter_rows(raw, profile))
    if profile.reverse:
        rows.reverse()
    return rows


def _iter_rows(raw: bytes, profile: CsvProfile) -> Iterator[StatementRow]:
    text = raw.decode(profile.encoding).lstrip("﻿")  # tolerate a BOM
    lines = text.splitlines()
    if profile.skip_preamble:
        lines = lines[profile.skip_preamble :]
    reader = csv.DictReader(lines)

    for lineno, row in enumerate(reader, start=2):  # header is line 1
        amount = _signed_amount(profile, row)

        if profile.currency_column:
            currency = (row.get(profile.currency_column) or "").strip().upper()
            # The signed amount is in the *account* currency; a non-GBP account
            # currency breaks the all-GBP assumption (brief §13) -- fail loudly
            # rather than silently sum mixed currencies. (A foreign point-of-sale
            # amount lives in a separate "local amount" column we ignore.)
            if currency and currency != "GBP":
                raise ValueError(
                    f"{profile.name}: row {lineno} account currency is "
                    f"{currency!r}, expected GBP"
                )

        name = (row.get(profile.name_column) or "").strip() if profile.name_column else ""
        desc = (
            (row.get(profile.description_column) or "").strip()
            if profile.description_column
            else ""
        )
        # Prefer the bank's description; fall back to the payee name when it is
        # blank (Monzo leaves Description empty on peer transfers).
        raw_desc = desc or name
        norm = normalise(f"{name} {desc}")
        payee_norm = payee_for(profile, name, desc)

        date = dt.datetime.strptime(row[profile.date_column].strip(), profile.date_format).date()
        time = (row.get(profile.time_column) or "").strip() if profile.time_column else ""
        source_id = (row.get(profile.id_column) or "").strip() if profile.id_column else ""
        hint = None
        if profile.category_hint_column:
            hint = (row.get(profile.category_hint_column) or "").strip() or None
        balance_after = None
        if profile.balance_column:
            balance_after = to_pence(row[profile.balance_column])

        yield StatementRow(
            date=date,
            amount_signed=amount,
            description_raw=raw_desc,
            description_norm=norm,
            payee_norm=payee_norm,
            time=time,
            source_id=source_id,
            category_hint=hint,
            balance_after=balance_after,
        )
