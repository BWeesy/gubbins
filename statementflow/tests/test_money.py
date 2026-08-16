# SPDX-License-Identifier: MIT
"""Money parsing: exact pence, no float drift."""

from __future__ import annotations

import pytest

from app.money import format_pence, to_pence


@pytest.mark.parametrize(
    ("text", "pence"),
    [
        ("-6.20", -620),
        ("1500.00", 150000),
        ("0.00", 0),
        ("2.00", 200),
        ("-0.87", -87),
        ("1,234.56", 123456),  # thousands separator tolerated
        (" -3.50 ", -350),  # surrounding whitespace tolerated
    ],
)
def test_to_pence(text, pence):
    assert to_pence(text) == pence


@pytest.mark.parametrize("bad", ["", "   ", "abc", "1.2.3"])
def test_to_pence_rejects_junk(bad):
    with pytest.raises(ValueError):
        to_pence(bad)


def test_format_pence():
    assert format_pence(-50000) == "-£500.00"
    assert format_pence(150000) == "£1,500.00"
    assert format_pence(0) == "£0.00"
    assert format_pence(-87) == "-£0.87"
