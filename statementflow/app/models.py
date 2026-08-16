# SPDX-License-Identifier: MIT
"""The internal, bank-agnostic transaction schema.

Every bank's export is normalised before anything downstream touches it. The
cardinal rule (brief §4): amounts are a single **signed value** -- credit
positive, debit negative -- regardless of how the source bank represents sign.
Downstream maths (net delta, reconciliation, the ecosystem invariant) assume it.
"""

from __future__ import annotations

from enum import Enum


class Category(str, Enum):
    """The **structural** categories -- assigned by a transaction's direction or
    pairing, never user-configured.

    INCOME and TRANSFER come from the flow's direction/pairing. OTHER is the
    static catch-all outflow bucket. UNCATEGORISED marks a debit awaiting a human
    bucket (surfaced by the review UI; shown as OTHER in the Sankey).

    The *specific* buckets (outflow: mortgage, energy, ...; income: salary,
    refund, ...) are configurable, not enum members -- see ``config.CategoryDef``.
    OTHER is the generic outflow bucket and INCOME the generic income bucket;
    both are assignable. TRANSFER and UNCATEGORISED are never rule targets.
    """

    INCOME = "income"
    TRANSFER = "transfer"
    OTHER = "other"
    UNCATEGORISED = "uncategorised"


#: Categories a rule/review can never assign. A transfer is assigned by pairing;
#: uncategorised is the "needs review" fallback. INCOME and OTHER are absent --
#: they are the generic income/outflow buckets and are valid targets.
NON_ASSIGNABLE = frozenset(
    {Category.TRANSFER.value, Category.UNCATEGORISED.value}
)

#: Sentinel owner for a joint account: a real, first-class value, because we do
#: not know which household member triggered the transaction (brief §2.4).
JOINT = "joint"
