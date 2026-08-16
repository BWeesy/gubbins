<!--
SPDX-License-Identifier: MIT
-->

# StatementFlow configuration reference

Normative reference for StatementFlow's configuration. Keep it in lockstep with
the code — a config change and its doc change land together.

Configuration is split into **public** and **private**:

| File                          | Committed? | Contents                                  |
|-------------------------------|------------|-------------------------------------------|
| `profiles/*.yaml`             | **public** | bank export layouts (no personal data)    |
| `config/users.yaml`           | private    | household members                         |
| `config/accounts.yaml`        | private    | in-ecosystem current/joint accounts       |
| `config/categories.yaml`      | private    | configurable outflow buckets              |
| `config/rules.yaml`           | private    | hand-authored classification rules        |
| `config/rules.learned.yaml`   | private    | app-written learned rules (never by hand) |

Only the `*.example.yaml` templates for the private files are committed. The
real files are gitignored.

## Where config is loaded from

`APP_CONFIG_DIR` names the directory holding the private config files. Default
`./config` in dev; `/var/lib/statementflow/config` under systemd. Config is user
intent — the app never writes it back, except learned rules, which go to a
separate `rules.learned.yaml` so hand-authored `rules.yaml` is never clobbered.

## `users.yaml`

A list of household members. `id` is a stable key referenced by accounts;
`name` is display-only. See `config/users.example.yaml`.

`email` is optional and doubles as the **access allowlist**. It must be the
login `tailscale serve` reports for that person — the account they signed in to
Tailscale with (find it with `tailscale status --json | grep -i loginname`).

- If **any** user sets an `email`, only callers whose identity matches one of
  them are served; everyone else gets a 403. Use this when your tailnet has
  other people, shared nodes or guest devices on it.
- If **no** user sets one, the tailnet remains the only boundary — any tailnet
  user is allowed (the documented baseline).

Matching is case-insensitive, and two users may not share an email. When a
caller is recognised, the audit trail records their configured `id` rather than
the raw login.

## `accounts.yaml`

The in-ecosystem current/joint account **nodes**. Savings and Investment are
sink *categories*, not accounts, and are never listed here. Per account:

- `id`, `label`, `bank` (which profile to parse its exports with)
- `owners` — one user id (sole account → ownership = that user) or two (joint
  account → ownership = the literal `joint`). Ownership is derived from this,
  never from who uploaded the file.
- `opening_balance` + `as_of` *(optional)* — the balance anchor, in integer
  pence, for banks whose export omits a running-balance column (see §5a of the
  brief). Running balances are reconstructed from it by cumulative signed sum.

See `config/accounts.example.yaml`.

## `categories.yaml`

The **configurable buckets** the Sankey splits flows into and that rules/review
can assign. Each entry has an `id` (lower-case, referenced by rules), a display
`label`, and a `kind`:

- `kind: outflow` (default) — spending buckets: `savings`, `mortgage`,
  `energy`, `tv`, … A debit is assigned one of these (or `other`).
- `kind: income` — income buckets: `salary`, `refund`, `benefits`, … A credit
  is assigned one of these (or the generic `income`). Income splits into these
  as separate source nodes in the Sankey.

See `config/categories.example.yaml`.

`transfer`, `other`, `income` and `uncategorised` are **structural** and always
exist — a configured id may not reuse them. `other`/`income` are the generic
outflow/income buckets and the defaults for an unmatched debit/credit;
`uncategorised` marks a row awaiting review. If `categories.yaml` is absent, the
defaults are `savings`/`bills`/`investment` (outflow) and a single generic
income bucket.

## `rules.yaml`

Hand-authored classification rules mapping a payee/description pattern to one of
`savings | bills | investment | other`, plus `known_destinations` marking your
own accounts that appear only as an unpaired debit leg (treated as transfers).
See `config/rules.example.yaml`.

**Match semantics.** Each `match` is a **case-insensitive substring** tested
against the transaction's normalised description (upper-cased, whitespace
collapsed). **First match wins**, and hand-authored rules are always tried
before learned ones, so a hand rule beats a later learned rule for the same
payee. A debit that matches no rule is left **uncategorised** and surfaced in
the review queue (it is treated as `other` for display in the Sankey). Only the
four outflow buckets are valid rule targets — `income`/`transfer` are structural
and never rule-assigned.

## `rules.learned.yaml` (app-written)

Written by the review UI's "remember this payee": assigning a bucket with
*remember* ticked appends `{match, bucket}` here (the match normalised so it
lines up with how descriptions are compared). The app only ever appends to this
file and never touches `rules.yaml` (brief §8). Private/gitignored — its entries
mirror real payees.

## Bank profiles (`profiles/*.yaml`)

Public, per-bank export layouts. Format documented in
[profiles/README.md](profiles/README.md). Confirm each against one real,
anonymised export before trusting it.

## Environment variables

- `APP_CONFIG_DIR` — directory of private config files (see above). Dev default
  `./config`; under systemd `/var/lib/statementflow/config`.
- `STATEMENTFLOW_DB` — path to the SQLite database. Default
  `./data/statementflow.db`.
- `STATEMENTFLOW_REQUIRE_IDENTITY` — when truthy (the **default**), data
  endpoints require the `Tailscale-User-Login` header, which only
  `tailscale serve` injects, so a direct hit on the loopback port is refused.
  Set it to `0` for local development without Tailscale (the static pages stay
  reachable; the JSON/data endpoints would otherwise 403).
  **This is not a CSRF defence:** `tailscale serve` stamps the header on every
  request it proxies, including one a site you are visiting triggered
  cross-origin. Endpoints taking a JSON body are incidentally protected (the
  content type forces a preflight); `POST /upload` takes a multipart form and is
  not, though an attacker would have to guess a private account id blind.
- `STATEMENTFLOW_RAW_DIR` — where uploaded statements are retained for
  re-import. Default `<db dir>/raw`.

Ownership is always derived from account config, never from the identity header.
Uploads are capped at 10 MB.
