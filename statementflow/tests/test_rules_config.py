# SPDX-License-Identifier: MIT
"""Phase 2: rule loading/matching, learned-rule writing, and config loading."""

from __future__ import annotations

import datetime as dt

import pytest
import yaml

from app.config import load_config
from app.models import JOINT
from app.rules import RULES_FILE, RuleSet, load_ruleset, remember_payee

# --- config --------------------------------------------------------------

def _write_config(directory, *, users, accounts):
    (directory / "users.yaml").write_text(yaml.safe_dump({"users": users}))
    (directory / "accounts.yaml").write_text(yaml.safe_dump({"accounts": accounts}))


def test_owner_derivation(tmp_path):
    _write_config(
        tmp_path,
        users=[{"id": "alice", "name": "Alice"}, {"id": "bob", "name": "Bob"}],
        accounts=[
            {"id": "sole", "label": "Monzo", "bank": "monzo", "owners": ["alice"]},
            {"id": "joint", "label": "Joint", "bank": "natwest", "owners": ["alice", "bob"]},
        ],
    )
    config = load_config(tmp_path)
    assert config.accounts["sole"].owner == "alice"
    assert config.accounts["joint"].owner == JOINT


def test_config_rejects_unknown_owner(tmp_path):
    _write_config(
        tmp_path,
        users=[{"id": "alice", "name": "Alice"}],
        accounts=[{"id": "x", "label": "X", "bank": "monzo", "owners": ["nobody"]}],
    )
    with pytest.raises(ValueError, match="not a known user"):
        load_config(tmp_path)


def test_config_reads_balance_anchor(tmp_path):
    _write_config(
        tmp_path,
        users=[{"id": "bob", "name": "Bob"}],
        accounts=[{
            "id": "ob", "label": "Other Bank", "bank": "otherbank",
            "owners": ["bob"], "opening_balance": 250000, "as_of": dt.date(2026, 1, 1),
        }],
    )
    account = load_config(tmp_path).accounts["ob"]
    assert account.opening_balance == 250000
    assert account.as_of == dt.date(2026, 1, 1)


# --- rules ---------------------------------------------------------------

def test_rule_matching_first_match_wins(tmp_path):
    (tmp_path / RULES_FILE).write_text(yaml.safe_dump({
        "rules": [
            {"match": "broker ltd", "bucket": "investment"},
            {"match": "energy co", "bucket": "bills"},
        ],
        "known_destinations": [{"match": "tfr to 12-34-56", "account_id": "ob"}],
    }))
    rs = load_ruleset(tmp_path)
    assert rs.bucket_for("BROKER LTD INVESTOR ACCOUNT") == "investment"
    assert rs.bucket_for("ENERGY CO DD") == "bills"
    assert rs.bucket_for("UNKNOWN PAYEE") is None
    assert rs.known_destination_for("TFR TO 12-34-56 12345678") == "ob"


def test_rules_reject_structural_bucket(tmp_path):
    # transfer/uncategorised are structural and can never be rule targets
    # (income and other ARE assignable -- the generic buckets).
    (tmp_path / RULES_FILE).write_text(yaml.safe_dump({
        "rules": [{"match": "xfer", "bucket": "transfer"}],
    }))
    with pytest.raises(ValueError, match="not an assignable category"):
        load_ruleset(tmp_path)


def test_remember_payee_writes_learned_not_hand(tmp_path):
    (tmp_path / RULES_FILE).write_text(yaml.safe_dump({
        "rules": [{"match": "broker ltd", "bucket": "investment"}],
    }))
    remember_payee(tmp_path, match="Big Shop", bucket="bills")

    # Hand-authored file untouched.
    hand = yaml.safe_load((tmp_path / RULES_FILE).read_text())
    assert hand == {"rules": [{"match": "broker ltd", "bucket": "investment"}]}

    # Learned rule is applied on next load, normalised to upper-case.
    rs = load_ruleset(tmp_path)
    assert rs.bucket_for("BIG SHOP 1234") == "bills"


def test_remember_payee_is_idempotent(tmp_path):
    remember_payee(tmp_path, match="Big Shop", bucket="bills")
    remember_payee(tmp_path, match="BIG SHOP", bucket="other")  # same match, requalified
    learned = yaml.safe_load((tmp_path / "rules.learned.yaml").read_text())
    matches = [r["match"] for r in learned["rules"]]
    assert matches == ["BIG SHOP"]  # not duplicated
    assert learned["rules"][0]["bucket"] == "other"  # updated in place


def test_hand_rule_beats_learned(tmp_path):
    (tmp_path / RULES_FILE).write_text(yaml.safe_dump({
        "rules": [{"match": "coffee", "bucket": "bills"}],
    }))
    remember_payee(tmp_path, match="coffee", bucket="other")
    rs = load_ruleset(tmp_path)
    # Hand rules load first and first-match wins.
    assert rs.bucket_for("COFFEE SHOP") == "bills"


def test_empty_ruleset_when_no_files(tmp_path):
    assert load_ruleset(tmp_path) == RuleSet(rules=(), known_destinations=())
