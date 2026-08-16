# SPDX-License-Identifier: MIT
"""Phase 2: transfer pairing + the classification pipeline (pure engine)."""

from __future__ import annotations

import datetime as dt

from app.engine import EngineTxn, classify
from app.models import Category
from app.rules import KnownDestination, Rule, RuleSet

D = dt.date


def _rs(rules=(), dests=()) -> RuleSet:
    return RuleSet(rules=tuple(rules), known_destinations=tuple(dests))


def _decisions_by_id(decisions):
    return {d.txn_id: d for d in decisions}


def test_pairs_equal_and_opposite_across_accounts():
    txns = [
        EngineTxn(1, "monzo-alice", D(2026, 5, 1), -20000, "TRANSFER"),
        EngineTxn(2, "joint-current", D(2026, 5, 2), 20000, "FROM ALICE"),
    ]
    by_id = _decisions_by_id(classify(txns, ruleset=_rs()))
    assert by_id[1].category == Category.TRANSFER
    assert by_id[1].counter_account_id == "joint-current"
    assert by_id[2].category == Category.TRANSFER
    assert by_id[2].counter_account_id == "monzo-alice"


def test_pairing_respects_the_date_window():
    txns = [
        EngineTxn(1, "monzo-alice", D(2026, 5, 1), -20000, "TRANSFER"),
        EngineTxn(2, "joint-current", D(2026, 5, 10), 20000, "FROM ALICE"),
    ]
    by_id = _decisions_by_id(classify(txns, ruleset=_rs(), pairing_window_days=3))
    # Too far apart to pair: both unmatched -> need review.
    assert by_id[1].category == Category.UNCATEGORISED
    assert by_id[2].category == Category.UNCATEGORISED


def test_same_account_equal_opposite_does_not_pair():
    txns = [
        EngineTxn(1, "monzo-alice", D(2026, 5, 1), -5000, "A"),
        EngineTxn(2, "monzo-alice", D(2026, 5, 1), 5000, "B"),
    ]
    by_id = _decisions_by_id(classify(txns, ruleset=_rs()))
    assert by_id[1].category == Category.UNCATEGORISED
    assert by_id[2].category == Category.UNCATEGORISED


def test_known_destination_debit_is_a_transfer():
    txns = [EngineTxn(1, "monzo-alice", D(2026, 5, 1), -60000, "TFR TO 12-34-56 12345678")]
    dests = [KnownDestination("TFR TO 12-34-56 12345678", "otherbank-bob")]
    by_id = _decisions_by_id(classify(txns, ruleset=_rs(dests=dests)))
    assert by_id[1].category == Category.TRANSFER
    assert by_id[1].counter_account_id == "otherbank-bob"


def test_unmatched_credit_needs_review():
    txns = [EngineTxn(1, "monzo-alice", D(2026, 5, 1), 150000, "ACME SALARY")]
    by_id = _decisions_by_id(classify(txns, ruleset=_rs()))
    assert by_id[1].category == Category.UNCATEGORISED


def test_credit_matching_an_income_rule_gets_the_income_category():
    txns = [EngineTxn(1, "monzo-alice", D(2026, 5, 1), 150000, "ACME SALARY")]
    rules = [Rule("ACME", "salary")]
    by_id = _decisions_by_id(classify(
        txns, ruleset=_rs(rules=rules), income_buckets={"salary", "income"}))
    assert by_id[1].category == "salary"


def test_outflow_rule_does_not_fire_on_a_credit():
    """A rule targeting an outflow bucket must not classify a credit."""
    txns = [EngineTxn(1, "monzo-alice", D(2026, 5, 1), 5000, "BROKER LTD DIVIDEND")]
    rules = [Rule("BROKER LTD", "investment")]
    by_id = _decisions_by_id(classify(
        txns, ruleset=_rs(rules=rules),
        outflow_buckets={"investment", "other"}, income_buckets={"income"}))
    assert by_id[1].category == Category.UNCATEGORISED  # not "investment"


def test_debit_matching_a_rule_gets_the_bucket():
    txns = [EngineTxn(1, "monzo-alice", D(2026, 5, 1), -10000, "BROKER LTD INVESTOR")]
    rules = [Rule("BROKER LTD", "investment")]
    by_id = _decisions_by_id(classify(txns, ruleset=_rs(rules=rules)))
    assert by_id[1].category == "investment"


def test_unmatched_debit_is_uncategorised():
    txns = [EngineTxn(1, "monzo-alice", D(2026, 5, 1), -1234, "MYSTERY SHOP")]
    by_id = _decisions_by_id(classify(txns, ruleset=_rs()))
    assert by_id[1].category == Category.UNCATEGORISED


def test_pairing_precedes_known_destination_and_rules():
    """A debit that would match a rule still classifies as a transfer when it has
    a real opposite leg -- pairing runs first, or transfers double-count."""
    txns = [
        EngineTxn(1, "monzo-alice", D(2026, 5, 1), -10000, "BROKER LTD"),
        EngineTxn(2, "joint-current", D(2026, 5, 1), 10000, "BROKER LTD"),
    ]
    rules = [Rule("BROKER LTD", "investment")]
    by_id = _decisions_by_id(classify(txns, ruleset=_rs(rules=rules)))
    assert by_id[1].category == Category.TRANSFER


def test_pairing_prefers_exact_timestamp_over_same_day_amount_clash():
    """Two same-day credits of the same amount land minutes apart: a genuine
    peer-transfer leg (posted the same second as the debit) and an unrelated
    payment. Pairing must pick the same-timestamp leg, not the nearby one --
    date alone cannot separate them."""
    DT = dt.datetime
    debit = EngineTxn(
        1, "monzo-alice", D(2026, 6, 2), -50000, "TRANSFER",
        posted_at=DT(2026, 6, 2, 12, 0, 0),
    )
    true_leg = EngineTxn(
        2, "joint-current", D(2026, 6, 2), 50000, "FROM ALICE",
        posted_at=DT(2026, 6, 2, 12, 0, 0),  # same second -> the true pair
    )
    decoy = EngineTxn(
        3, "joint-current", D(2026, 6, 2), 50000, "DECOY SAME DAY SAME AMOUNT",
        posted_at=DT(2026, 6, 2, 12, 1, 0),  # a minute later, unrelated
    )
    by_id = _decisions_by_id(classify([debit, decoy, true_leg], ruleset=_rs()))
    assert by_id[1].counter_account_id == "joint-current"
    assert by_id[2].category == Category.TRANSFER   # true leg paired
    assert by_id[3].category == Category.UNCATEGORISED  # decoy left unpaired -> review


def test_greedy_pairing_picks_nearest_date():
    txns = [
        EngineTxn(1, "monzo-alice", D(2026, 5, 5), -20000, "OUT"),
        EngineTxn(2, "joint-current", D(2026, 5, 4), 20000, "NEAR"),   # 1 day away
        EngineTxn(3, "joint-current", D(2026, 5, 1), 20000, "FAR"),    # 4 days away
    ]
    by_id = _decisions_by_id(classify(txns, ruleset=_rs(), pairing_window_days=5))
    assert by_id[1].counter_account_id == "joint-current"
    assert by_id[2].category == Category.TRANSFER   # the near one paired
    assert by_id[3].category == Category.UNCATEGORISED  # the far one left unpaired -> review
