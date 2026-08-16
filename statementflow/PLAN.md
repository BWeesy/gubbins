<!--
SPDX-License-Identifier: MIT
-->

# StatementFlow — plan of attack

Build plan for the StatementFlow module: a self-hosted monthly cash-flow Sankey
for a household, served over Tailscale. This document is the working plan; the
authoritative *design* is the project brief. Where this plan and the brief
disagree, the brief wins — raise the discrepancy rather than quietly diverging.

StatementFlow lives in the `gubbins` monorepo alongside `glowbridge` and inherits
its engineering conventions (see repo-root `SKILLS.md`): `uv`-run with pinned
lockfiles, `ruff` clean, tests against synthetic fixtures and fakes (never live
banks or real statements), a normative `CONFIG.md` kept in lockstep with the
code, an SPDX `MIT` header on every source file, and UK English comments that
explain *why*.

## The dominant constraint: this is a public repository

`gubbins` is public on GitHub. **No personal or financial data may ever be
committed — not a real statement, not a real account number, sort code, payee,
balance, or name.** This constraint outranks every other goal in this plan. It
shapes the very first phase and every phase after it.

What is public vs private:

| Public (committed)                         | Private (gitignored, never committed)                 |
|--------------------------------------------|-------------------------------------------------------|
| Bank **profiles** (column layouts only)    | `config/*.yaml` real accounts / users / rules         |
| `*.example.yaml` config templates          | `rules.learned.yaml` (app-written)                    |
| App code, tests, docs                      | The SQLite DB (`*.db`), `data/`, `uploads/`           |
| **Synthetic** test fixtures                | Retained raw statements                               |
|                                            | `.env`                                                |

If confirming a bank profile requires a real export, that export is anonymised
and inspected **locally only** — it never enters the working tree.

## Module structure — small package (decided)

`glowbridge` is a single-file PEP 723 script. StatementFlow has parsers, a
classification/pairing engine, bank profiles, a FastAPI API, a DB, and a web
frontend — too much for one file. It is a **small package**, still `uv`-run and
still lockfile-pinned, laid out as:

```
statementflow/
  app/                     # FastAPI app, parsers, engine, /flows endpoint
    __init__.py
    main.py                # FastAPI app + Tailscale-identity middleware
    db.py                  # SQLite schema + access
    models.py              # internal signed-value transaction schema
    profiles.py            # load + apply public bank profiles
    parse.py               # CSV/OFX/QIF → normalised transactions
    engine.py              # classification pipeline + transfer pairing
    flows.py               # month → nodes + weighted edges + net_delta
    config.py              # load users/accounts/rules from APP_CONFIG_DIR
  profiles/                # PUBLIC bank profiles (monzo.yaml, natwest.yaml, ...)
  config/
    users.example.yaml
    accounts.example.yaml
    rules.example.yaml
  tests/                   # synthetic fixtures ONLY — never real statements
  static/                  # index.html + ECharts glue (CDN, no npm build)
  nix/                     # systemd service module
  CONFIG.md                # normative config reference
  README.md
  PLAN.md                  # this file
```

Real config is loaded from `APP_CONFIG_DIR` (dev default `./config`; under
systemd `/var/lib/statementflow/config`).

## Locked design decisions (from the brief — do not re-litigate)

1. **Ecosystem boundary.** Only everyday current/joint accounts are nodes.
   Savings and Investment are *sink categories*, not accounts — their statements
   are never uploaded as sources. Avoids double-counting.
2. **Transfers** are current→current only, detected by pairing equal-and-opposite
   legs, or by a debit to a *known own destination* when only one leg exists.
3. **Classification is config-driven.** Hand-written rules map payees to the four
   outflow buckets; the review screen handles the rest and can learn new rules.
4. **Ownership is derived from the account's configured `owners`, not the
   uploader.** Sole account → that user; joint account → the literal value
   `joint`. The uploader's Tailscale identity is recorded for audit only.
5. **Auth = the tailnet.** No passwords. App binds `127.0.0.1`; `tailscale serve`
   terminates TLS and injects the identity header. Trust `Tailscale-User-Login`
   *only* because `tailscale serve` is the sole ingress.

## Classification pipeline (per transaction, in order — §4 of the brief)

1. Paired with an own-account opposite leg (amount matches, dates within N days,
   both in-ecosystem) → **Transfer** (both legs collapse to one edge).
2. Else debit to a known own destination → **Transfer** (unpaired leg).
3. Else credit from external → **Income**.
4. Else debit → apply rules → **Savings / Bills / Investment / Other** (default
   **Other**).

All amounts normalise to a **signed value** (credit +, debit −) regardless of how
each bank represents them.

## Net delta + reconciliation (§5a — the subtle part, test it hard)

Each account node shows its net change over the window:
`net_delta = inflows − outflows = closing − opening`, computed **two independent
ways**:

1. **Flow sum** — `sum(amount_signed)` for the account in the window. Always
   available.
2. **Balance diff** — `closing − opening`, when a running-balance column exists
   or the account has a configured `opening_balance` + `as_of` anchor.

Disagreement between (1) and (2) means missing/duplicated transactions → surface
a **reconciliation warning** on that node. Edge cases to get right:

- Running balance is *after* each transaction, so
  `opening = first_txn.balance − first_txn.amount_signed`, `closing =
  last_txn.balance`.
- Resolve same-day ordering deterministically via `seq`.
- A month's opening should equal the prior month's closing (another check).
- **Ecosystem invariant:** across all in-ecosystem accounts, transfers cancel,
  so `Σ(account deltas) = total income − total sink outflows`. Whole-diagram
  sanity check.

## Data model (SQLite — §6)

```
imports        (id, uploaded_by_user_id, uploaded_at, bank_profile,
                source_hash, row_count)
transactions   (id, import_id, account_id, owner,
                date, seq, amount_signed, balance_after NULL,
                description_raw, description_norm,
                dedup_hash, category, counter_account_id NULL, reviewed BOOL)
```

- **Dedup** on `dedup_hash = hash(account_id, date, amount, description_norm)` →
  overlapping re-uploads are idempotent. Different accounts with equal/opposite
  amounts are *not* dupes — pairing is a separate step from dedup.
- **Transfer pairing** sets `counter_account_id` on both legs.
- `balance_after` optional (populated when the profile maps a balance column);
  `seq` disambiguates same-day ordering for opening/closing derivation.

## Build order (front-loads the risk — §12)

### Phase 0 — Guardrails first (before any code touches real data)
- `.gitignore` block: `statementflow/data/`, `*.db`, `statementflow/config/*.yaml`
  (except `*.example.yaml`), `statementflow/uploads/`, `rules.learned.yaml`,
  retained raw statements, `.env`.
- Module skeleton with **only** public/example files (structure above).
- CI workflow gated on `statementflow/**`: ruff + tests + lock-drift + dependency
  audit, mirroring `.github/workflows/glowbridge.yml`, **plus a guard that fails
  if real config or statement data is ever staged** (belt-and-braces on the
  public-repo risk).

### Phase 1 — Data model + one parser + import + dedup
- SQLite schema per §6. Bank **profile format** (public YAML): column mapping,
  date format, sign convention, optional running-balance column, header preamble
  to skip.
- **Monzo parser first** (clean CSV, has a balance column + a category hint).
  Normalise into the signed-value internal schema. Dedup → idempotent re-import.
- Confirm the Monzo profile against one real anonymised export, locally.

### Phase 2 — Rules engine + transfer pairing + review UI
- Config-driven rules; `rules.yaml` (hand-authored) + `rules.learned.yaml`
  (app-written) kept **separate** so learning never clobbers hand rules.
- Classification pipeline (§4) + transfer pairing (`counter_account_id` on both
  legs) + owner derivation from account `owners`.
- Review UI: `category IS NULL`/low-confidence table, assign dropdown, "remember
  payee" writes a learned rule.

### Phase 3 — `/flows` endpoint + Sankey frontend
- `GET /flows?month=YYYY-MM` → nodes + weighted edges + per-account `net_delta`
  (+ `opening`/`closing`/`reconciliation` when balance data present).
- Frontend: plain HTML + **ECharts from CDN**, no npm build, month selector, node
  labels showing net delta (`Monzo −£500`).

### Phase 4 — Remaining banks + partner accounts
- Add a profile per remaining bank as its statements are onboarded. Expect the
  usual variations: a header preamble to skip, a different `date_format`, and
  banks that omit a running balance (those need the `opening_balance` + `as_of`
  anchor fallback). Confirm every profile against one real anonymised export
  before trusting it — never assume a layout.

### Phase 5 — NixOS deployment
- `systemd.services.statementflow` (`DynamicUser = true`, `StateDirectory =
  "statementflow"`), `tailscale serve https → http://127.0.0.1:PORT`, bind
  `127.0.0.1` only, add the state dir to the existing Restic backup set.

## Open decisions (recommendation given — decide during the relevant phase)

- **Income representation** (Phase 3): recommend v1 aggregates into a small number
  of income nodes (optionally per person + joint). Splitting by source can come
  later.
- **Person split** (post-v1): optionally colour/split the Sankey three ways (each
  person + `joint`), rendering `joint` as its own lane by default. Nice-to-have,
  not v1.

## Testing posture

Concentrate coverage where the subtle bugs live — the same philosophy as
glowbridge:

- Signed-value normalisation across every bank's sign convention.
- Dedup idempotency across overlapping re-uploads.
- Transfer pairing (equal-and-opposite, date window, unpaired known-destination)
  and the *non*-pairing of equal/opposite amounts across unrelated accounts.
- Net-delta reconciliation: flow sum vs balance diff, the running-balance edge
  cases, same-day `seq` ordering, month-boundary continuity, the ecosystem
  invariant.
- Owner derivation (sole vs joint) — never from the uploader.
- Config validation (unknown keys fatal, like glowbridge).

**All fixtures are synthetic. No live-bank tests, no real statements in the tree,
ever.**

## Stack

- **Backend:** FastAPI + SQLite, `uv`-run, lockfile-pinned.
- **Frontend:** plain HTML + ECharts/Plotly from CDN, no npm build step.
- **Assumptions:** all GBP; two users to start, but the user list is data, not
  code.
