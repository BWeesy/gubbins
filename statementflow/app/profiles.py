# SPDX-License-Identifier: MIT
"""CSV bank profiles: the PUBLIC, per-bank description of a CSV export's layout.

A profile is generic YAML (brief §7): which columns are date/amount/description,
the date format, the sign convention, an optional running-balance column
(enables the §5a reconciliation check), any header preamble to skip, and the
per-bank behaviour strategies (see ``strategies.py``). Profiles carry **no
personal data** and live in ``statementflow/profiles/`` in the public repo.

Every profile must be confirmed against one real, anonymised export before being
trusted -- do not assume column layouts (brief §7). Banks whose export capability
is limited especially need verifying before a layout is assumed.

This is the CSV profile specifically. Other source formats the brief anticipates
(QIF; PDF later) would get their own sibling profile type -- hence ``CsvProfile``
rather than a generic name.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import yaml

from .strategies import DEDUP_STRATEGIES, PAYEE_STRATEGIES

#: Directory holding the public bank profiles.
PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"


@dataclass(frozen=True, slots=True)
class CsvProfile:
    """One bank's CSV export layout. Field names are the YAML keys.

    ``signed_amount_column`` is the single column carrying a +/- amount in the
    account currency. ``id_column`` names a stable per-transaction id when the
    bank provides one (Monzo does). ``balance_column`` is present only when the
    export carries a running balance -- its absence is why Monzo cannot use the
    §5a balance-diff reconciliation.

    ``reverse`` is set when the export lists newest-first (NatWest), so rows are
    flipped to chronological order before ``seq`` and opening/closing are derived.

    ``dedup_strategy`` and ``payee_strategy`` name behaviours from
    ``strategies.py`` (each declares the profile fields it requires, validated
    below).
    """

    name: str
    date_column: str
    date_format: str
    format: str = "csv"
    encoding: str = "utf-8"
    skip_preamble: int = 0
    reverse: bool = False
    # Sign convention -- exactly one of: a single signed amount column, OR a
    # pair of positive-magnitude debit/credit columns (Lloyds, Nationwide, ...).
    signed_amount_column: str | None = None
    debit_column: str | None = None
    credit_column: str | None = None
    id_column: str | None = None
    time_column: str | None = None
    currency_column: str | None = None
    name_column: str | None = None
    description_column: str | None = None
    category_hint_column: str | None = None
    balance_column: str | None = None
    dedup_strategy: str = "source_id"
    payee_strategy: str = "name_or_description"


def load_profile(name: str, directory: Path | None = None) -> CsvProfile:
    """Load and validate a CSV bank profile by name (e.g. "monzo").

    Unknown keys are fatal, with the offending key named -- the same strict
    stance glowbridge takes on config, so a typo surfaces loudly instead of
    being silently ignored. Each declared strategy's required fields are checked
    against the registry in ``strategies.py``.
    """
    path = (directory or PROFILES_DIR) / f"{name}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"profile {name!r} must be a YAML mapping")

    known = {f.name for f in fields(CsvProfile)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(
            f"profile {name!r} has unknown key(s): {', '.join(sorted(unknown))}"
        )
    if raw.get("format", "csv") != "csv":
        raise ValueError(
            f"profile {name!r}: this is a CSV profile; unsupported format "
            f"{raw.get('format')!r}"
        )

    # Exactly one sign convention: a signed column, or a debit/credit pair.
    has_signed = bool(raw.get("signed_amount_column"))
    has_debit_credit = bool(raw.get("debit_column")) and bool(raw.get("credit_column"))
    if has_signed == has_debit_credit:
        raise ValueError(
            f"profile {name!r}: set exactly one sign convention -- either "
            f"signed_amount_column, or both debit_column and credit_column"
        )

    dedup = raw.get("dedup_strategy", "source_id")
    if dedup not in DEDUP_STRATEGIES:
        raise ValueError(
            f"profile {name!r}: dedup_strategy must be one of "
            f"{sorted(DEDUP_STRATEGIES)}, got {dedup!r}"
        )
    missing = [f for f in DEDUP_STRATEGIES[dedup].required_fields if not raw.get(f)]
    if missing:
        raise ValueError(
            f"profile {name!r}: dedup_strategy {dedup!r} requires: "
            f"{', '.join(missing)}"
        )

    payee = raw.get("payee_strategy", "name_or_description")
    if payee not in PAYEE_STRATEGIES:
        raise ValueError(
            f"profile {name!r}: payee_strategy must be one of "
            f"{sorted(PAYEE_STRATEGIES)}, got {payee!r}"
        )
    return CsvProfile(**raw)
