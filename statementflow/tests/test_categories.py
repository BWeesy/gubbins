# SPDX-License-Identifier: MIT
"""Configurable outflow categories: loading, rule validation, and Sankey sinks.

income/transfer/other/uncategorised stay structural; the specific outflow buckets
(savings, mortgage, energy, ...) come from categories.yaml.
"""

from __future__ import annotations

import datetime as dt

import pytest
import yaml

from app.config import CategoryDef, Config, load_config
from app.db import connect, init_db
from app.flows import flows_for_range
from app.rules import load_ruleset


def _write(directory, categories=None):
    (directory / "users.yaml").write_text(yaml.safe_dump({"users": [{"id": "a", "name": "A"}]}))
    (directory / "accounts.yaml").write_text(yaml.safe_dump({"accounts": [
        {"id": "acc", "label": "Acc", "bank": "monzo", "owners": ["a"]}]}))
    if categories is not None:
        (directory / "categories.yaml").write_text(yaml.safe_dump({"categories": categories}))


# --- config loading ------------------------------------------------------

def test_absent_categories_file_uses_defaults(tmp_path):
    _write(tmp_path)
    config = load_config(tmp_path)
    assert set(config.categories) == {"savings", "bills", "investment"}


def test_configured_categories_load(tmp_path):
    _write(tmp_path, categories=[
        {"id": "mortgage", "label": "Mortgage"},
        {"id": "energy", "label": "Energy"},
        {"id": "salary", "label": "Salary", "kind": "income"},
    ])
    config = load_config(tmp_path)
    assert set(config.categories) == {"mortgage", "energy", "salary"}
    assert config.categories["salary"].kind == "income"
    # Outflow buckets add the static OTHER; income buckets add the generic INCOME.
    assert config.outflow_buckets() == {"mortgage", "energy", "other"}
    assert config.income_buckets() == {"salary", "income"}


def test_income_kind_categories(tmp_path):
    _write(tmp_path, categories=[{"id": "dividends", "label": "Dividends", "kind": "income"}])
    config = load_config(tmp_path)
    assert config.income_buckets() == {"dividends", "income"}
    assert config.outflow_buckets() == {"other"}  # no outflow categories configured


def test_bad_kind_is_rejected(tmp_path):
    _write(tmp_path, categories=[{"id": "x", "label": "X", "kind": "sideways"}])
    with pytest.raises(ValueError, match="kind must be"):
        load_config(tmp_path)


def test_reserved_category_id_is_rejected(tmp_path):
    _write(tmp_path, categories=[{"id": "income", "label": "Nope"}])
    with pytest.raises(ValueError, match="reserved"):
        load_config(tmp_path)


# --- rule validation against configured categories -----------------------

def test_rule_bucket_must_be_a_configured_category(tmp_path):
    (tmp_path / "rules.yaml").write_text(yaml.safe_dump({
        "rules": [{"match": "acme", "bucket": "mortgage"}]}))
    # 'mortgage' is not configured here -> rejected.
    with pytest.raises(ValueError, match="not a configured category"):
        load_ruleset(tmp_path, valid_buckets={"energy", "other"})
    # ...and accepted when it is configured.
    rs = load_ruleset(tmp_path, valid_buckets={"mortgage", "other"})
    assert rs.bucket_for("ACME MORTGAGE") == "mortgage"


# --- Sankey sinks are data-driven ----------------------------------------

def _config():
    return Config(
        users={}, accounts={},
        categories={cid: CategoryDef(cid, cid.title()) for cid in ("mortgage", "energy")},
    )


def _income_config():
    return Config(
        users={}, accounts={},
        categories={
            "salary": CategoryDef("salary", "Salary", "income"),
            "refund": CategoryDef("refund", "Refunds", "income"),
        },
    )


def test_income_categories_are_separate_source_nodes(tmp_path):
    conn = connect(tmp_path / "i.db")
    init_db(conn)
    conn.execute("INSERT INTO imports(uploaded_by_user_id, uploaded_at, bank_profile, "
                 "source_hash, row_count) VALUES ('a','t','monzo','h',1)")
    for i, (amt, cat) in enumerate([(320000, "salary"), (5000, "refund"), (1000, "uncategorised")]):
        conn.execute(
            "INSERT INTO transactions(import_id, account_id, owner, date, seq, amount_signed, "
            "description_raw, description_norm, payee_norm, dedup_hash, category) "
            "VALUES (1,'acc','a','2026-05-10',?,?,'d','d','d',?,?)", (i, amt, f"h{i}", cat))
    conn.commit()

    flows = flows_for_range(conn, _income_config(), dt.date(2026, 5, 1), dt.date(2026, 5, 31))
    incomes = {n["id"]: n["label"] for n in flows["nodes"] if n["type"] == "income"}
    assert incomes["salary"] == "Salary"
    assert incomes["refund"] == "Refunds"
    assert incomes["income"] == "Income"     # the uncategorised credit shows as generic income
    edges = {(e["source"], e["target"]): e["value"] for e in flows["edges"]}
    assert edges[("salary", "acc")] == 3200.0
    assert edges[("income", "acc")] == 10.0   # the uncategorised credit


def test_custom_category_appears_as_its_own_sink(tmp_path):
    conn = connect(tmp_path / "c.db")
    init_db(conn)
    conn.execute("INSERT INTO imports(uploaded_by_user_id, uploaded_at, bank_profile, "
                 "source_hash, row_count) VALUES ('a','t','monzo','h',1)")
    for i, (amt, cat) in enumerate([(-120000, "mortgage"), (-8000, "energy"), (-3000, "uncategorised")]):
        conn.execute(
            "INSERT INTO transactions(import_id, account_id, owner, date, seq, amount_signed, "
            "description_raw, description_norm, payee_norm, dedup_hash, category) "
            "VALUES (1,'acc','a','2026-05-10',?,?,'d','d','d',?,?)", (i, amt, f"h{i}", cat))
    conn.commit()

    flows = flows_for_range(conn, _config(), dt.date(2026, 5, 1), dt.date(2026, 5, 31))
    sinks = {n["id"]: n["label"] for n in flows["nodes"] if n["type"] == "sink"}
    assert sinks["mortgage"] == "Mortgage"      # configured label
    assert sinks["energy"] == "Energy"
    assert sinks["other"] == "Other"            # the uncategorised debit shows as other
    edges = {(e["source"], e["target"]): e["value"] for e in flows["edges"]}
    assert edges[("acc", "mortgage")] == 1200.0
    assert edges[("acc", "other")] == 30.0
