# SPDX-License-Identifier: MIT
"""Money parsing. Amounts live as integer minor units (pence) everywhere; only
the display edge converts to pounds. Parsing goes through Decimal so a value
like "-6.20" never touches binary float."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def to_pence(value: str) -> int:
    """Parse a decimal pounds string (e.g. "-6.20", "1500.00") to integer pence.

    Raises ValueError on anything that is not a clean decimal amount. Thousands
    separators are tolerated; a trailing/leading sign and surrounding whitespace
    are fine.
    """
    text = value.strip().replace(",", "")
    if not text:
        raise ValueError("empty amount")
    try:
        pounds = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a decimal amount: {value!r}") from exc
    return int((pounds * 100).to_integral_value(rounding=ROUND_HALF_UP))


def format_pence(pence: int) -> str:
    """Format integer pence as a signed pounds string, e.g. -50000 -> "-£500.00"."""
    sign = "-" if pence < 0 else ""
    whole, minor = divmod(abs(pence), 100)
    return f"{sign}£{whole:,}.{minor:02d}"
