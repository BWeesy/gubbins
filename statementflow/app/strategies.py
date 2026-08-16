# SPDX-License-Identifier: MIT
"""Per-bank behaviour strategies for CSV profiles.

Two behaviours vary between banks and are pluggable here, so onboarding a bank
(or adding a behaviour) touches one file: how a row is keyed for idempotent
dedup, and how the merchant/payee token is extracted. Strategies are
behaviour-named, not bank-named, and each declares the profile fields it
requires, which the profile loader validates.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .text import normalise

if TYPE_CHECKING:
    from .parse import StatementRow
    from .profiles import CsvProfile


# --- dedup: how a row is keyed for idempotent re-import ------------------

@dataclass(frozen=True, slots=True)
class DedupStrategy:
    """Keys a row for dedup. ``required_fields`` are profile attributes that must
    be set for the strategy to work (validated at profile load). ``key_parts``
    returns the tuple hashed into the key (the account id is always part of it,
    so keys never collide across accounts)."""

    name: str
    required_fields: tuple[str, ...]
    key_parts: Callable[[CsvProfile, str, StatementRow], list[str]]


def _source_id_key(profile: CsvProfile, account_id: str, row: StatementRow) -> list[str]:
    """The bank's stable transaction id (e.g. Monzo)."""
    if not row.source_id:
        raise ValueError(
            f"{profile.name}: dedup strategy 'source_id' but a row on "
            f"{row.date.isoformat()} has no id -- cannot key it safely"
        )
    return ["source_id", account_id, row.source_id]


def _running_balance_key(profile: CsvProfile, account_id: str, row: StatementRow) -> list[str]:
    """(date, amount, balance) for a bank with a running balance but no id
    (e.g. NatWest): two otherwise-identical same-day rows leave different
    balances and stay distinct."""
    if row.balance_after is None:
        raise ValueError(
            f"{profile.name}: dedup strategy 'running_balance' but a row on "
            f"{row.date.isoformat()} has no balance -- cannot key it safely"
        )
    return [
        "running_balance",
        account_id,
        row.date.isoformat(),
        str(row.amount_signed),
        str(row.balance_after),
    ]


DEDUP_STRATEGIES: dict[str, DedupStrategy] = {
    s.name: s
    for s in (
        DedupStrategy("source_id", ("id_column",), _source_id_key),
        DedupStrategy("running_balance", ("balance_column",), _running_balance_key),
    )
}


def dedup_hash(profile: CsvProfile, account_id: str, row: StatementRow) -> str:
    """The idempotency key for a row, per the profile's ``dedup_strategy``.
    There is deliberately no generic fallback -- a naive
    (account, date, amount, description) hash silently drops genuine same-day
    duplicates."""
    strategy = DEDUP_STRATEGIES.get(profile.dedup_strategy)
    if strategy is None:
        raise ValueError(
            f"{profile.name}: unsupported dedup strategy {profile.dedup_strategy!r}"
        )
    parts = strategy.key_parts(profile, account_id, row)
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


# --- payee: how the merchant/payee token is extracted -------------------

@dataclass(frozen=True, slots=True)
class PayeeStrategy:
    """Extracts the payee token (what "remember payee" learns on) from a row's
    ``name`` and ``description``. It must be the generalisable merchant part, not
    the whole description."""

    name: str
    extract: Callable[[str, str], str]


def _name_or_description(name: str, description: str) -> str:
    """The Name column where the bank provides one (e.g. Monzo), else the whole
    description."""
    return normalise(name) if name.strip() else normalise(description)


# A card-transaction description starts with the card's last 4 digits + a DDMMMYY
# date, e.g. "1234 30JUN26" (optionally "... C").
_CARD_PREFIX = re.compile(r"^\d{4}\s+\d{2}[A-Z]{3}\d{2}")


def _delimited_description(name: str, description: str) -> str:
    """Merchant from a comma-delimited description (e.g. NatWest). A card row
    (``1234 30JUN26 C , CORNER GARAGE , FUEL , FAKETOWN GB``) drops its card/date
    prefix and trailing location, keeping the middle (``CORNER GARAGE FUEL``);
    other rows keep the first field (``PHONE CO LTD`` / ``PARTNER TRANSFER``)."""
    parts = [p.strip() for p in description.split(",") if p.strip()]
    if not parts:
        return ""
    if _CARD_PREFIX.match(parts[0]):
        middle = parts[1:-1] if len(parts) >= 3 else parts[1:]
        return normalise(" ".join(middle)) or normalise(parts[0])
    return normalise(parts[0])


PAYEE_STRATEGIES: dict[str, PayeeStrategy] = {
    s.name: s
    for s in (
        PayeeStrategy("name_or_description", _name_or_description),
        PayeeStrategy("delimited_description", _delimited_description),
    )
}


def payee_for(profile: CsvProfile, name: str, description: str) -> str:
    """The payee token for a row, per the profile's ``payee_strategy``."""
    return PAYEE_STRATEGIES[profile.payee_strategy].extract(name, description)
