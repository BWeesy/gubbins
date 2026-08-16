# SPDX-License-Identifier: MIT
"""The /flows query: a date range -> Sankey nodes + weighted edges + net deltas.

Backs ``GET /flows?start=YYYY-MM-DD&end=YYYY-MM-DD`` (brief §5), over any
inclusive date range -- not just a calendar month. Three layers:

    Income -> Account, Account <-> Account (transfers), Account -> Sink

plus, per account, ``net_delta`` and -- when balance data is present --
``opening`` / ``closing`` / ``reconciliation`` (brief §5a).

Net delta is computed two independent ways and cross-checked:

  1. flow sum  = sum(amount_signed) over the account in the window (always there)
  2. balance diff = closing - opening (needs a running-balance column; Monzo has
     none, so this stays None for Monzo and reconciliation is skipped)

Disagreement means missing/duplicated transactions -> a reconciliation warning
on that node. Whole-diagram sanity check: transfers cancel, so
Σ(account deltas) = total income - total sink outflows.

Money stays in integer pence until the response is built; pounds are only for
display. A transfer contributes exactly one edge (its debit leg); the credit leg
is skipped so it is not double-counted. An uncategorised debit is shown as
``other`` (the brief's default) while remaining in the review queue.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from sqlite3 import Connection

from .config import Config
from .models import Category
from .money import format_pence

INCOME_NODE = "income"
OTHER_SINK = Category.OTHER.value


def data_date_range(conn: Connection) -> dict:
    """The earliest and latest transaction dates present (ISO strings), or
    ``{min: None, max: None}`` when there is no data. Lets the UI default and
    bound its date pickers."""
    row = conn.execute(
        "SELECT MIN(date) AS lo, MAX(date) AS hi FROM transactions"
    ).fetchone()
    return {"min": row["lo"], "max": row["hi"]}


def _sink_for(category: str, split_unreviewed: bool) -> str:
    """Which sink an outflow debit falls into. An uncategorised debit shows as
    OTHER (brief §4 default) -- unless ``split_unreviewed``, when it stays its own
    node so the review-status view can highlight it. Any other category value is
    its own sink, so sinks are data-driven."""
    if category == Category.UNCATEGORISED.value:
        return category if split_unreviewed else OTHER_SINK
    return category


def _reconcile(rows: list) -> tuple[int | None, int | None, int | None]:
    """Return (opening, closing, balance_diff) in pence, or Nones when the account
    has no running-balance data for every row in the window.

    Running balance is *after* each transaction, so the opening balance is the
    first row's balance minus its own amount (brief §5a). Reconciliation needs a
    balance on every row, or a gap would make the diff meaningless.
    """
    if not rows or any(r["balance_after"] is None for r in rows):
        return None, None, None
    opening = rows[0]["balance_after"] - rows[0]["amount_signed"]
    closing = rows[-1]["balance_after"]
    return opening, closing, closing - opening


def flows_for_range(
    conn: Connection,
    config: Config,
    start: dt.date,
    end: dt.date,
    *,
    transfers: str = "include",
    account_ids: set[str] | None = None,
    split_unreviewed: bool = False,
) -> dict:
    """Build the Sankey payload for the inclusive date range ``[start, end]``.

    ``transfers`` = include | exclude | only (which edges to draw).
    ``account_ids`` restricts to those accounts (an owner's, for per-owner lanes).
    ``split_unreviewed`` keeps uncategorised rows as their own node instead of
    merging into the generic OTHER/INCOME, so a review-status view can highlight
    them. The ``summary`` block (per owner and per account) is the true net
    position for the window and is unaffected by the ``transfers`` display filter.
    """
    where = "date >= ? AND date <= ?"
    params: list = [start.isoformat(), end.isoformat()]
    if account_ids is not None:
        where += f" AND account_id IN ({','.join('?' * len(account_ids))})"
        params += sorted(account_ids)
    rows = conn.execute(
        "SELECT account_id, amount_signed, balance_after, category, "
        f"counter_account_id FROM transactions WHERE {where} ORDER BY date, seq",
        params,
    ).fetchall()

    income_edges: dict[tuple[str, str], int] = defaultdict(int)  # (income cat, account)
    transfer_edges: dict[tuple[str, str], int] = defaultdict(int)
    account_sink: dict[tuple[str, str], int] = defaultdict(int)
    flow_sum: dict[str, int] = defaultdict(int)
    rows_by_account: dict[str, list] = defaultdict(list)
    accounts: set[str] = set()
    # Per-account net-position accumulators for the summary.
    _blank = {"income": 0, "outflow": 0, "transfers_in": 0, "transfers_out": 0}
    summ: dict[str, dict[str, int]] = defaultdict(lambda: dict(_blank))

    for r in rows:
        acc, amt, cat = r["account_id"], r["amount_signed"], r["category"]
        accounts.add(acc)
        flow_sum[acc] += amt
        rows_by_account[acc].append(r)
        s = summ[acc]

        if cat == "transfer":
            if amt < 0:  # the debit leg carries the single collapsed edge
                dst = r["counter_account_id"] or "external"
                transfer_edges[(acc, dst)] += -amt
                s["transfers_out"] += -amt
            else:
                s["transfers_in"] += amt
        elif amt > 0:
            income_edges[(_income_source_for(cat, split_unreviewed), acc)] += amt
            s["income"] += amt
        elif amt < 0:
            account_sink[(acc, _sink_for(cat, split_unreviewed))] += -amt
            s["outflow"] += -amt

    # Any account referenced only as a transfer destination still needs a node.
    for _src, dst in transfer_edges:
        accounts.add(dst)

    show_income = transfers != "only"
    show_sinks = transfers != "only"
    show_transfers = transfers != "exclude"

    # Each edge is attributed to the account it touches (income -> its target
    # account; account -> sink and transfers -> their source account), so the
    # frontend can colour flows by that account or its owner.
    edges = []
    if show_income:
        for (src, acc), pence in income_edges.items():
            edges.append(_edge(src, acc, pence, account=acc))
    if show_transfers:
        for (src, dst), pence in transfer_edges.items():
            edges.append(_edge(src, dst, pence, account=src))
    if show_sinks:
        for (acc, sink), pence in account_sink.items():
            edges.append(_edge(acc, sink, pence, account=acc))

    order = {cid: i for i, cid in enumerate(config.categories)}

    nodes = []
    present_income = {src for src, _acc in income_edges} if show_income else set()
    inc_order = {**order, INCOME_NODE: len(order)}
    for src in sorted(present_income, key=lambda s: (inc_order.get(s, len(order) + 1), s)):
        nodes.append({"id": src, "label": _income_label(config, src), "type": "income"})
    for acc in sorted(accounts):
        opening, closing, balance_diff = _reconcile(rows_by_account.get(acc, []))
        account = config.accounts.get(acc)
        node = {
            "id": acc,
            "label": _account_label(config, acc),
            "type": "account",
            # Owner (a user id or 'joint') from the account config, for the
            # colour-by-owner view. None for an account referenced only as a
            # transfer destination and not in config.
            "owner": account.owner if account else None,
            "net_delta_pence": flow_sum.get(acc, 0),
            "net_delta": format_pence(flow_sum.get(acc, 0)),
        }
        if balance_diff is not None:
            node["opening"] = format_pence(opening)
            node["closing"] = format_pence(closing)
            node["reconciliation"] = {
                "ok": balance_diff == flow_sum.get(acc, 0),
                "flow_sum": format_pence(flow_sum.get(acc, 0)),
                "balance_diff": format_pence(balance_diff),
            }
        nodes.append(node)
    present_sinks = {sink for _acc, sink in account_sink} if show_sinks else set()
    sink_order = {**order, OTHER_SINK: len(order)}
    for sink in sorted(present_sinks, key=lambda s: (sink_order.get(s, len(order) + 1), s)):
        nodes.append({"id": sink, "label": _sink_label(config, sink), "type": "sink"})

    total_income = sum(income_edges.values())
    total_sinks = sum(account_sink.values())
    net_all = sum(flow_sum.values())
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "nodes": nodes,
        "edges": edges,
        "totals": {
            "income": format_pence(total_income),
            "sinks": format_pence(total_sinks),
            # Transfers cancel across the ecosystem, so Σ deltas should equal
            # income - sinks. A mismatch usually means a transfer's other leg
            # falls outside the selected range (brief §5a).
            "invariant_ok": net_all == total_income - total_sinks,
        },
        "summary": _summary(config, summ, flow_sum),
    }


def _summary(
    config: Config, per_account: dict[str, dict[str, int]], flow_sum: dict[str, int]
) -> dict:
    """Net position per account and per owner (income, outflow, transfers in/out,
    net) in pence -- the exact numbers the Sankey shows qualitatively."""
    by_account: dict[str, dict] = {}
    by_owner: dict[str, dict] = {}
    for acc, s in per_account.items():
        owner = _owner(config, acc) or "unknown"
        entry = {**s, "net": flow_sum.get(acc, 0)}
        by_account[acc] = {**entry, "label": _account_label(config, acc), "owner": owner}
        agg = by_owner.setdefault(owner, dict(_BLANK_SUMMARY))
        for k in ("income", "outflow", "transfers_in", "transfers_out"):
            agg[k] += s[k]
    for owner, agg in by_owner.items():
        agg["net"] = (
            agg["income"] - agg["outflow"] + agg["transfers_in"] - agg["transfers_out"]
        )
    return {"by_owner": by_owner, "by_account": by_account}


_BLANK_SUMMARY = {"income": 0, "outflow": 0, "transfers_in": 0, "transfers_out": 0}


def _edge(source: str, target: str, pence: int, *, account: str) -> dict:
    return {
        "source": source,
        "target": target,
        "value": round(pence / 100, 2),
        "value_label": format_pence(pence),
        "account": account,  # the account this flow is attributed to
    }


def _account_label(config: Config, account_id: str) -> str:
    account = config.accounts.get(account_id)
    return account.label if account else account_id


def _owner(config: Config, account_id: str) -> str | None:
    account = config.accounts.get(account_id)
    return account.owner if account else None


def transactions_for(
    conn: Connection,
    config: Config,
    start: dt.date,
    end: dt.date,
    *,
    category: str,
    account_ids: set[str] | None = None,
) -> list[dict]:
    """The transactions behind a Sankey node, for drill-down. ``category`` is a
    node id; the generic OTHER/INCOME nodes also fold in the uncategorised rows of
    the matching direction (mirroring how the Sankey builds them)."""
    where = ["date >= ?", "date <= ?"]
    params: list = [start.isoformat(), end.isoformat()]

    if category == OTHER_SINK:  # generic outflow: reviewed 'other' + uncategorised debits
        where.append("((category = ?) OR (category = 'uncategorised' AND amount_signed < 0))")
        params.append(OTHER_SINK)
    elif category == INCOME_NODE:  # generic income: 'income' + uncategorised credits
        where.append("((category = ?) OR (category = 'uncategorised' AND amount_signed > 0))")
        params.append(INCOME_NODE)
    else:
        where.append("category = ?")
        params.append(category)

    if account_ids is not None:
        where.append(f"account_id IN ({','.join('?' * len(account_ids))})")
        params += sorted(account_ids)

    rows = conn.execute(
        "SELECT account_id, date, amount_signed, description_raw, payee_norm, "
        f"category FROM transactions WHERE {' AND '.join(where)} "
        "ORDER BY date, seq",
        params,
    ).fetchall()
    return [
        {
            "date": r["date"],
            "account": _account_label(config, r["account_id"]),
            "amount": format_pence(r["amount_signed"]),
            "amount_pence": r["amount_signed"],
            "description": r["description_raw"],
            "payee": r["payee_norm"],
            "category": r["category"],
        }
        for r in rows
    ]


def _income_source_for(category: str, split_unreviewed: bool) -> str:
    """Which income source node a credit belongs to. An uncategorised credit
    shows as the generic INCOME (brief §4 default) -- unless ``split_unreviewed``,
    when it stays its own node. Symmetric to ``_sink_for``."""
    if category == Category.UNCATEGORISED.value:
        return category if split_unreviewed else INCOME_NODE
    return category


def _sink_label(config: Config, sink: str) -> str:
    """A configured category's label, or a title-cased fallback (covers OTHER and
    any category present in data but not in config)."""
    category = config.categories.get(sink)
    if category is not None:
        return category.label
    return sink.replace("_", " ").title()


def _income_label(config: Config, source: str) -> str:
    """Label for an income source node -- 'Income' for the generic bucket, else
    the configured label or a title-cased fallback."""
    if source == INCOME_NODE:
        return "Income"
    category = config.categories.get(source)
    return category.label if category is not None else source.replace("_", " ").title()
