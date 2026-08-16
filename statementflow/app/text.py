# SPDX-License-Identifier: MIT
"""Text normalisation, shared by parsing and the strategy layer."""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Collapse whitespace and upper-case, for stable matching and dedup. Bank
    descriptions pad with runs of spaces, so this must flatten them or the same
    payee compares differently across rows."""
    return _WHITESPACE.sub(" ", text).strip().upper()
