# Working on this repository

Orientation for anyone (human or agent) picking up work here. It captures the
things that are not obvious from reading a single file: the invariants that
must not be broken, why the code is shaped the way it is, and how to develop
and test it. Read this before making changes.

glowbridge is a **Home Assistant long-term statistics importer**
(`recorder/import_statistics` over the HA WebSocket), version **1.0.0**.
Design background and the reasoning behind the shape live in the gitignored
`glowbridge/notes/DESIGN-V2.md`.

## Repository layout

```
glowbridge/       the bridge (single-file app + its tests and docs)
  glowbridge.py           the daemon; one file, PEP 723 inline deps
  test_glowbridge.py      tests; imports glowbridge by path
  CONFIG.md               normative config reference — keep in lockstep with code
  README.md               design rationale and troubleshooting
  glowbridge.example.toml annotated example config
  dev_scripts/            one-off exploration tools, not part of the shipped app
  notes/                  gitignored working notes / design drafts
.github/          CI workflows and issue templates
```

`glowbridge` is the project. The repo is a personal "tools around the house"
monorepo, so treat `glowbridge/` as self-contained.

## What glowbridge is

A single-file Python daemon that polls the Glowmarkt/DCC API (the backend
behind Hildebrand's Bright app for UK smart meters) and imports consumption
into Home Assistant as **long-term statistics**, stamped with each interval's
*true* time. It exists to be a *correct* and *good-citizen* replacement for
HACS integrations that mis-time consumption and hammer a free API. **Data
correctness beats data freshness everywhere.**

Incoming data is per-window energy: each API row is `[interval_start_epoch,
kwh]`. We fetch hourly (`period=PT1H`), keep a synthetic **monotonic
cumulative total** per meter (integer Wh), and import hourly `{start, sum}`
rows via `recorder/import_statistics`. HA derives each hour's consumption by
differencing consecutive sums, and each row carries its true `start`, so
consumption appears in the hour it actually happened regardless of when the
import runs — that correct-time placement is the point of the whole design.
HA long-term statistics are hourly; that is the resolution ceiling and it's
HA's, not ours.

## Load-bearing invariants — do not break these

These encode failure analysis, not preference.

1. **The state file is the single source of truth.** Per-resource frontier +
   cumulative Wh, Glow auth token and resource cache live in one JSON file
   (atomic write → fsync → rename, mode 0600). The bridge **reads back
   nothing from HA, ever** — HA is a write-only sink. Do not add
   recover-state-from-HA logic; owning the cumulative locally is what keeps
   imports consistent.
2. **We own the cumulative `sum`; it is monotonic by construction.** HA does
   not accumulate for us — for a `has_sum` statistic we supply the cumulative
   total per hour and HA differences it. Seed the cumulative at 0 before the
   first backfilled hour so deltas are correct. The total never decreases.
3. **Imports are idempotent and revisable — exploit it, don't fight it.** A
   re-import overwrites by `(statistic_id, start)`. Late data and gap-fills
   are handled by re-importing the affected contiguous slice (from the
   earliest changed hour forward, since `sum` is cumulative). This is why
   there is **no exactly-once bookkeeping** — re-import does that job — and
   why every cycle re-fetches the trailing `revision_window`.
4. **Statistic IDs and the state-file schema are ABI.** `statistic_id`s
   (`glowbridge:electricity_consumption`, `glowbridge:gas_consumption`) are
   ABI the moment history sits behind them — renaming orphans it. Bump the
   state `schema` int on a format change; an unrecognised schema is treated
   as a fresh install, never silently reinterpreted.
5. **Secrets never reach logs.** All output passes a redacting formatter
   (configured passwords + the HA token scrubbed, token/password JSON fields
   masked; urllib3 pinned at INFO). Debug logs must stay safe to paste into
   an issue.
6. **Auth floor (Glow).** A minimum interval between Glow auth attempts,
   persisted across restarts, so a crash loop cannot become an auth loop (the
   upstream locks accounts). At most one re-auth per cycle on token rejection.
   The HA token is a long-lived token from the env; `auth_invalid` from HA is
   a config error surfaced once, never retried in a loop.
7. **Cycle failure is never daemon-fatal.** An exhausted retry budget skips
   the cycle; the daemon records failure status and waits. The big first-run
   backfill is *resumable* (state saved per chunk), so a partial run is
   success-so-far, not a failure.
8. **Cost/tariff resources are never bridged.** Only `electricity.consumption`
   and `gas.consumption`. Cost is derivable in HA; the API's tariff data is
   unreliable and has broken whole integrations.

## The import model (frontier + cumulative + revision window)

State per resource is a **settled frontier** (`settled_through`, the hour up
to which data is final and never re-fetched) plus `cumulative_wh_at_settled`
(the running total at that hour, so forward sums recompute from a known
baseline without storing per-hour energy).

- **Backfill is the normal first run.** Fresh state seeds the frontier at
  `now − backfill_lookback` (default 365 d) and walks forward to now. Backfill
  and steady state are one code path, differing only in range size.
- **Fetch in ≤30-day chunks** (`PT1H`'s per-request limit is 31 days), with
  per-chunk retry (backoff + jitter, `Retry-After` on 429) and a gentle
  pause. Save state after each chunk → resumable.
- **`offset=0`** (UTC) so the API's hour boundaries match HA's UTC statistics
  hours.
- **Finalisation lag** keeps the freshest, most-volatile hour out of the
  import. **Revision window** (default 3 d) is re-fetched and re-imported each
  cycle so DCC revisions / late gap-fills self-correct; nothing older is
  re-touched.

## Failure-visibility model

Three conditions stay distinguishable via a **status file** (`status.json`
next to `state.json`, rewritten each cycle) plus systemd:

| Condition | Signal |
|---|---|
| Process dead | systemd unit failed / not active |
| Bridge alive, API failing | status file `consecutive_failures` > 0 |
| Bridge & API fine, DCC feed stale | status file `data_complete_to` lags now |

HA can surface the status file via a `command_line`/`file` sensor. Most
"missing data" reports are the DCC being behind, not the bridge — keep that
distinction cheap to make.

## Configuration contract

- Single strict TOML file. **Unknown keys anywhere are fatal** with the full
  path; wrong types are fatal (bool rejected where int expected).
- Semantic checks at startup (interval floor, `revision_window` sane, a
  valid `[homeassistant].url`, etc.).
- **Env vars carry secrets only** and override the file:
  `GLOWBRIDGE_GLOW_USERNAME/PASSWORD`, `GLOWBRIDGE_HA_TOKEN`. Nothing else is
  env-settable. Config is user intent; the program never writes it back.
- Durations are bare integer seconds. No duration strings. (Timestamps, if
  any, are validated ISO — but the backfill window is a *duration*.)
- `glowbridge/CONFIG.md` is normative — update it in the same change as any
  config code.

## State file

- Carries a `schema` int. An *unrecognised* schema is treated as a fresh
  install, which triggers the backfill (idempotent, harmless). Nothing
  crash-loops on bad state.
- When you change the persisted shape: bump `STATE_SCHEMA`, add migration or a
  documented clean-break, and add a test.

## Running it (dev loop)

Always run via uv, never a bare interpreter (PEP 723 deps):

```sh
uv run --script glowbridge/glowbridge.py --dry-run    # fetch + print, no import, no state writes
uv run --script glowbridge/glowbridge.py --dump-raw   # dump raw API payloads to JSON for inspection
uv run --script glowbridge/glowbridge.py --once       # one real cycle (imports to HA), then exit
uv run --script glowbridge/glowbridge.py              # daemon
```

- `--dry-run` is side-effect-free: safe first check that auth/discovery/fetch
  work against the live API.
- End-to-end needs a reachable HA WebSocket + a long-lived token. Over
  Tailscale/localhost the link is already encrypted, so `ws://` is correct;
  use `wss://` for untrusted paths.

## Testing

```sh
uv run glowbridge/test_glowbridge.py    # or: pytest
ruff check glowbridge/                  # keep clean
```

- Concentrate coverage where the subtle bugs are: cumulative-sum math and the
  seed-at-zero first row; backfill chunking + **resumability**; revision
  re-import (a changed hour shifts later sums); fresh-start on an old schema;
  `finalisation_lag = 0`; config validation; redaction (incl. the HA token);
  the status file shape.
- The Glow API and the HA WebSocket are exercised through **fakes** (a fake
  in-process WS server for import). **No live-API/HA tests; none should be
  added.**
- Keep new logic in pure functions taking `now`/injected dependencies so it
  stays testable this way.

## Tooling and conventions

- Single file with PEP 723 inline metadata; `requires-python >= 3.12`; deps
  are `requests` and `websocket-client` (sync — fits the threaded daemon).
- Script deps are pinned with `uv lock --script`; CI checks the lock matches.
  Re-lock and commit the lockfile when deps change.
- SPDX `MIT` header line at the top of each source file.
- CI runs ruff (pinned) + tests + lock-drift + dependency audit. Keep it
  green. Actions are pinned to Node-24 majors.
- Prefer editing existing structures over adding parallel ones. Keep the
  single-file shape unless it genuinely breaks down.
- Comments/docs are maintainer-to-reader: explain *why*, especially for
  load-bearing ordering. Technical, specific, UK English.

## Platform gotchas

- The `#!/usr/bin/env -S uv run --script` shebang does not work on Windows;
  invoke `uv run --script` explicitly there. Deployment target is Linux/NixOS.
- `.gitattributes` pins `eol=lf` so the shebang survives commits from any OS.
- On Windows/NTFS the config file reads as world-readable, so the "chmod 600
  recommended" warning fires harmlessly; it's meaningful on Linux.
- uv's managed Pythons do not run on NixOS. The NixOS module wraps a **nixpkgs
  Python** (`python3.withPackages [ requests websocket-client ]`) and runs the
  script directly — no uv at runtime.

## Deployment (NixOS)

- **No broker or intermediate service** — glowbridge imports straight to HA
  over WebSocket, so there is nothing else to run.
- Secrets go in a root-only env file referenced by systemd `EnvironmentFile`
  (read as root before dropping to a `DynamicUser`), never in `configuration.nix`
  or the Nix store. Carries `GLOWBRIDGE_HA_TOKEN` and the Glow credentials.
- Flake-first: `nixosModules.glowbridge`, `packages.default`, a VM test in
  `checks`. The module is a plain importable `.nix` so classic
  `configuration.nix` can consume it via pinned `fetchTarball`.

## Out of scope

Multi-account, InfluxDB/Prometheus outputs, half-hourly resolution (HA
statistics are hourly), and cost/tariff sensors are excluded. Historical
backfill is *in* scope — it is the first-run behaviour (seed the frontier
`backfill_lookback` in the past and walk forward).

## dev_scripts

`glowbridge/dev_scripts/` is where any one-off exploration tools live, not
shipped code. Raw-payload inspection is built into the app as `--dump-raw`,
so the directory currently holds no scripts. `glowbridge/dev_scripts/*.json`
is gitignored: raw dumps are personal consumption data and must never be
committed.
