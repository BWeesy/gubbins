<!--
SPDX-License-Identifier: MIT
-->

# StatementFlow

A self-hosted, single-household web app that turns uploaded bank statements into
a **cash-flow Sankey** over any date range: income entering your current accounts,
transfers between them, and outflows bucketed into **Savings, Bills, Investment,
Other**. Reachable only over your tailnet.

Status: **early build.** See [PLAN.md](PLAN.md) for the phased build order. The
authoritative design is the project brief.

> **This is a public repository. Real financial data must never be committed.**
> Bank *profiles* (column layouts) are public; real accounts/users/rules, the
> database, uploads and retained raw statements are gitignored. Tests use
> synthetic fixtures only.

## How it works (the short version)

- Every transaction is normalised to a **signed value** (credit +, debit −) in
  integer pence, regardless of how each bank writes it.
- A config-driven pipeline classifies each row: paired own-account legs →
  **Transfer**; external credit → **Income**; other debits → the four outflow
  buckets via rules (default **Other**). A review UI handles the rest and learns
  new rules.
- `GET /flows?start=YYYY-MM-DD&end=YYYY-MM-DD` returns Sankey nodes + weighted edges, plus each
  account's **net delta**, cross-checked against its balance change as a free
  data-quality signal.

## Configuration

Real config is loaded from `APP_CONFIG_DIR` (dev default `./config`). Copy the
templates and edit them; the copies are gitignored:

```sh
cp config/users.example.yaml    config/users.yaml
cp config/accounts.example.yaml config/accounts.yaml
cp config/rules.example.yaml    config/rules.yaml
```

[CONFIG.md](CONFIG.md) is the normative reference — kept in lockstep with the code.

## Development

Everything runs through [uv](https://docs.astral.sh/uv/); dependencies are
pinned in `uv.lock`.

```sh
cd statementflow
uv run --locked pytest                 # tests (--locked also catches lock drift)
uvx ruff@0.15.22 check .               # lint

# Run the app (loopback only -- tailscale serve is the sole ingress in prod).
# In production the app requires the Tailscale identity header; for local dev
# without Tailscale, disable that check so the data endpoints are reachable:
STATEMENTFLOW_REQUIRE_IDENTITY=0 \
  uv run --locked python -m uvicorn app.main:app --host 127.0.0.1 --port 8770

# Re-lock after changing dependencies in pyproject.toml:
uv lock
```

## Security model

No passwords. The app binds `127.0.0.1` only; `tailscale serve` terminates TLS
and injects the caller's identity as `Tailscale-User-Login`, trusted **only**
because it is the sole path in. In production the app **requires** that header
(`STATEMENTFLOW_REQUIRE_IDENTITY=1`), so a request that did not arrive through
`tailscale serve` is rejected.

Beyond that, give each person in `users.yaml` an `email` — the login their
tailnet account uses. Once any user has one, only those identities are served
and every other caller gets a 403, so a tailnet with other people, shared nodes
or guest devices on it still only exposes the app to the household. With no
emails set the tailnet itself remains the only boundary. See
[CONFIG.md](CONFIG.md).

Two limits worth knowing: the header is injected by the proxy on *every* request
it forwards, so it is not a CSRF defence (a site the user visits can still
trigger a form POST); and it is only unforgeable to the extent that nothing
untrusted runs on the host itself.

## Deployment (NixOS)

A flake at the repo root exposes `nixosModules.statementflow` and a
`statementflow` package (a nixpkgs Python running the app — no uv at runtime),
plus a VM test in `checks`.

Add the module to your host and enable it:

```nix
{
  imports = [ inputs.gubbins.nixosModules.statementflow ];
  services.tailscale.enable = true;            # serve is the only ingress
  services.statementflow = {
    enable = true;
    # port defaults to 8770 (loopback only); tailscaleServe defaults to true.
  };
}
```

Then supply the **private** config (never committed, never in the Nix store —
real accounts and payees). After the first `nixos-rebuild switch`:

```sh
sudo cp users.yaml accounts.yaml categories.yaml rules.yaml \
    /var/lib/statementflow/config/
sudo chown -R statementflow:statementflow /var/lib/statementflow/config
sudo systemctl restart statementflow
```

The app is then reachable from any device on your tailnet at
**`https://<host>.<tailnet>.ts.net/`** (HTTPS — `tailscale serve` provisions the
cert). The SQLite DB, retained raw statements and learned rules live under
`/var/lib/statementflow`; add that to your existing Restic set.

`nix flake check` builds the package and runs the VM test (boots the module,
asserts the loopback-only bind and the identity gate).
