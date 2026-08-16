<!--
SPDX-License-Identifier: MIT
-->

# Bank profiles

A **profile** is a public, per-bank description of how that bank's export is
laid out. Profiles carry **no personal data** — only column positions, formats
and conventions — so they live in the public repo. One YAML file per bank
(`monzo.yaml`, `natwest.yaml`, …).

> **Confirm a profile against a real, anonymised export before relying on it**
> (brief §7). Bank export layouts change and vary by product; check what a real
> statement actually produces rather than trusting an assumed layout. Any such
> export is inspected **locally** — it is never committed.

## Format

```yaml
# monzo.yaml (illustrative — verify against a real export before use)
name: monzo
format: csv                     # only "csv" is supported so far
encoding: utf-8
skip_preamble: 0                # header/preamble lines to drop before the header row

date_column: "Date"
date_format: "%d/%m/%Y"         # strptime format as the bank writes it
time_column: "Time"             # optional; orders same-day rows (seq)

# Sign convention -- set EXACTLY ONE of these:
#   * a single signed amount column, OR
#   * a pair of positive-magnitude debit/credit columns (Lloyds, Nationwide, ...):
#       debit_column: "Debit Amount"
#       credit_column: "Credit Amount"
signed_amount_column: "Amount"  # a single +/- column in the account currency
currency_column: "Currency"     # optional; asserted == GBP

name_column: "Name"             # payee; used when Description is blank
description_column: "Description"
category_hint_column: "Category"  # optional; the bank's own category, a rules hint

# Present only when the export carries a running balance. Its presence enables
# the §5a balance-diff reconciliation, and backs the running_balance dedup
# strategy. Monzo omits it; NatWest has it.
# balance_column: "Balance"

# Set when the export lists newest-first (NatWest); rows are flipped to
# chronological order before seq / opening-closing are derived.
# reverse: true

# How rows are keyed for idempotent re-import, and how the payee token is
# derived. Both differ per bank, so each profile declares its own (see below).
dedup_strategy: source_id
id_column: "Transaction ID"     # required by the source_id strategy
payee_strategy: name_or_description
```

### `dedup_strategy` (per bank)

Overlapping re-uploads must be idempotent, but the field that uniquely
identifies a row is bank-specific, so each profile states its own:

| Strategy          | Key                                              | Use when |
|-------------------|--------------------------------------------------|----------|
| `source_id`       | the bank's stable transaction id (needs `id_column`) | the export carries a stable per-transaction id (e.g. Monzo) |
| `running_balance` | (account, date, amount, balance) (needs `balance_column`) | no stable id, but a running balance (e.g. NatWest): the post-transaction balance differs between two otherwise-identical same-day rows |

The account id is always part of the key, so keys never collide across accounts.

There is deliberately **no generic fallback** strategy. A naive
(account, date, amount, description) hash cannot tell two genuine same-day,
same-amount, same-payee transactions apart and would silently drop one — real
data loss. Banks without a stable id or a running balance get a purpose-built
strategy worked out when they are onboarded.

### `payee_strategy` (per bank)

The payee token is what "remember this payee" learns on, so it must be the
generalisable merchant part — not the whole description.

| Strategy               | Behaviour |
|------------------------|-----------|
| `name_or_description`  | the bank's Name column, else the description (e.g. Monzo) |
| `delimited_description`  | parse a comma-delimited description (e.g. NatWest): drop a `1234 30JUN26` card/date prefix and trailing location, keep the merchant |

Extraction is best-effort; the review UI lets the user edit the token before
learning, which covers the cases a heuristic gets wrong.

The parser (`app/parse.py`) applies a profile to a raw export and yields
`app.models.Transaction` values in the internal signed-value schema (integer
pence). All per-bank quirks are absorbed here so everything downstream sees one
uniform shape.
