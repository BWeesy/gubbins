# Working on this repository

Orientation for anyone (human or agent) picking up work here. It captures the
things that are not obvious from reading a single file: the invariants that
must not be broken, why the code is shaped the way it is, and how to develop
and test it. Read this before making changes.

## Repository layout

```
glowbridge/       the bridge (single-file app + its tests and docs)
  glowbridge.py           the daemon; one file, PEP 723 inline deps
  test_glowbridge.py      tests; imports glowbridge by path
  CONFIG.md               normative config reference — keep in lockstep with code
  README.md               design rationale and troubleshooting
  glowbridge.example.toml annotated example config
dev_scripts/      one-off exploration tools, not part of the shipped app
.github/          CI workflows and issue templates
```

`glowbridge` is the project. The repo is a personal "tools around the house"
monorepo, so treat `glowbridge/` as self-contained.

## What glowbridge is

A single-file Python daemon that polls the Glowmarkt/DCC API (the backend
behind Hildebrand's Bright app for UK smart meters) and publishes consumption
data to MQTT with Home Assistant discovery. It exists to be a *correct* and
*good-citizen* replacement for HACS integrations that corrupt Home Assistant
statistics and hammer a free API. **Data correctness beats data freshness
everywhere.**

Incoming data is per-window energy: each API row is `[interval_start_epoch, kwh]`,
the energy consumed in that half-hour. Outgoing is a synthetic **monotonic
cumulative total** per meter (integer watt-hours internally, published as kWh),
which is exactly what Home Assistant's `state_class: total_increasing` consumes.
The absolute value is meaningless; only deltas matter.

## Load-bearing invariants — do not break these

These encode failure analysis, not preference. If a change appears to simplify
one of these away, it is reintroducing a known bug.

1. **The state file is the single source of truth.** Watermark/ledger,
   cumulative Wh, auth token and resource cache live in one JSON file (atomic
   write → fsync → rename, mode 0600). The bridge **subscribes to nothing,
   ever** — it is a pure publisher. Retained MQTT topics are a copy for
   observability only. Do not add recover-state-from-broker logic: a stale
   retained watermark silently skipping data is the exact failure class this
   removes.
2. **Exactly-once, monotonic emission.** Every finalised half-hour is folded
   into the cumulative total at most once and the total never decreases. This
   is the property that makes it safe for `total_increasing`. Everything in the
   emission logic exists to protect it.
3. **Write ordering: state file → sensor state → status topics.** A crash
   mid-sequence must yield a gap, never a double-count, and the broker must stay
   conservative (never claim more than was published).
4. **Retained flags are deliberate.** Discovery configs, availability (LWT),
   and status topics are retained; **sensor `state` is never retained** — a
   stale retained total re-ingested after an HA restart looks like a meter
   reset.
5. **ABI, once released.** The MQTT topic layout, the discovery `unique_id`
   scheme (`glowbridge_{resource_id with - → _}`), and the state-file schema are
   ABI. Bump the `schema` int and migrate forward; never reinterpret an existing
   schema. Changing topic/unique_id layout orphans users' HA entities.
6. **Secrets never reach logs.** All output passes a redacting formatter
   (configured passwords scrubbed, token/password JSON fields masked; paho and
   urllib3 pinned at INFO). Debug logs must stay safe to paste into an issue.
   Never put credentials on an MQTT topic.
7. **Auth floor.** A minimum interval between auth attempts, persisted across
   restarts, so a crash loop cannot become an auth loop (the upstream locks
   accounts over aggressive re-auth). At most one re-auth per cycle on token
   rejection.
8. **Cycle failure is never daemon-fatal.** An exhausted retry budget skips the
   cycle; the daemon publishes failure status and waits for the next one.
9. **Cost/tariff resources are never bridged.** Only `electricity.consumption`
   and `gas.consumption`. Cost is derivable in HA; the API's tariff data is
   unreliable and has broken whole integrations.

## The emission model (frontier + ledger)

The core of the design lives in `select_emittable`. State per resource is a
**contiguous frontier** (the watermark) plus a **ledger** of window starts
already emitted *ahead* of it.

- **Finalisation lag:** a window is only eligible once its end is at least
  `finalisation_lag` in the past. Fresh DCC data can be revised; once emitted, a
  window is never revisited, so this lag is the revision guard.
- Any finalised window at/above the frontier not already in the ledger is
  emitted **on sight, in any order** — a missing half-hour does not withhold
  later ones.
- The frontier advances only across a contiguous run of accounted-for windows,
  draining the ledger as it goes, and is published as `data_complete_to`.
- A late-arriving window is emitted exactly once and the frontier sweeps over
  it. The DCC does backfill gaps, sometimes days later — this is normal and is
  why the frontier refetches from itself every cycle.
- A gap older than `heal_horizon` is written off (the frontier steps over it,
  losing that one window) so a permanently-undelivered interval cannot freeze
  the frontier forever. This also bounds the ledger.

Consequence to know: HA statistics cannot be backfilled over MQTT. Late data
lands at *arrival* time, so a large catch-up smears into the current day's
statistics. The cumulative stays exact; only daily attribution smears. Accepted
limitation.

## Failure-visibility model

Three conditions must stay distinguishable from the broker alone:

| Condition | Signal |
|---|---|
| Process dead | availability (LWT) → `offline` |
| Bridge alive, API failing | `consecutive_failures` > 0, `next_planned_update` pushed out |
| Bridge & API fine, DCC feed stale | `last_success` recent but `data_complete_to` lagging |

That last distinction — bridge broken vs DCC constipated — is the one that
matters when triaging missing data, and most "missing data" reports are the
DCC, not the bridge.

## Configuration contract

- Single strict TOML file. **Unknown keys anywhere are fatal** with the full
  path in the error; wrong types are fatal (bool is rejected where int is
  expected). Silently-ignored config is worse than a crash.
- Semantic checks at startup (e.g. interval floor, qos ∈ {0,1}, TLS options
  require `enabled`, `heal_horizon` must exceed `finalisation_lag`).
- **Env vars carry secrets only** and override the file; nothing else is
  env-settable. Config is user intent; the program never writes it back.
- Durations are bare integer seconds. No duration strings.
- `glowbridge/CONFIG.md` is normative — update it in the same change as any
  config code.

## State file

- Carries a `schema` int. On load: migrate forward in place (preserving
  cumulative totals so an upgrade emits no spurious meter-reset); an
  *unrecognised* schema, or a missing/corrupt/unreadable file, is logged and
  treated as a fresh install (reseed, cumulative restarts, one HA meter-reset
  event). Nothing crash-loops on bad state.
- When you change the persisted shape: bump `STATE_SCHEMA`, add a migration
  branch, and add a migration test. Never reinterpret an old schema in place.

## Running it (dev loop)

The script carries its own dependencies (PEP 723). Always run via uv, never a
bare interpreter:

```sh
uv run --script glowbridge/glowbridge.py --dry-run   # fetch + print, no MQTT, no state writes
uv run --script glowbridge/glowbridge.py --once      # one real cycle, then exit
uv run --script glowbridge/glowbridge.py             # daemon
```

- `--dry-run` is side-effect-free: safe first check that auth/discovery/fetch
  work against the live API.
- For an end-to-end broker check, run a local MQTT broker (Mosquitto), start a
  subscriber, then trigger a cycle. **Sensor `state` is not retained**, so the
  subscriber must be listening *before* the cycle publishes or you will only see
  the retained status/discovery topics.
- `--once` publishes its `offline` LWT on exit, so it always ends `offline`.
  Availability tracks a long-running daemon; don't drive a real deployment with
  repeated `--once`.

## Testing

```sh
uv run glowbridge/test_glowbridge.py    # or: pytest
ruff check glowbridge/                  # keep clean
```

- Coverage is concentrated where the subtle bugs are: DST transition days (a
  civil day is 46 or 50 half-hour slots at the boundaries), gap handling and
  out-of-order/late arrival, monotonicity, crash ordering (state persists even
  when the publisher explodes), auth floor across restarts, redaction, config
  validation, and state migration.
- Network and broker are exercised through **fakes**. There are **no live-API
  tests and none should be added.**
- Keep new logic in pure functions taking `now`/injected dependencies so it
  stays testable this way. When a code path can move state without emitting
  (e.g. a horizon write-off), test that the movement is persisted.

## Tooling and conventions

- Single file with PEP 723 inline metadata; `requires-python >= 3.12`; deps are
  `requests` and `paho-mqtt >= 2` (uses `CallbackAPIVersion.VERSION2` — do not
  "fix" the callbacks back to the v1 API).
- Script deps are pinned with `uv lock --script`; CI checks the lock matches.
  Run the lock command and commit the lockfile when deps change.
- SPDX `MIT` header line at the top of each source file.
- CI runs ruff + tests + lock-drift + dependency audit. Keep it green.
- Prefer editing existing structures over adding parallel ones. Keep the
  single-file shape unless it genuinely breaks down.
- Comments and docs are maintainer-to-reader: explain *why*, especially for
  load-bearing ordering and anything a well-meaning contributor might
  "simplify" into a bug. Technical, specific, UK English.

## Platform gotchas

- The `#!/usr/bin/env -S uv run --script` shebang does not work on Windows;
  invoke `uv run --script` explicitly there. The deployment target is
  Linux/NixOS.
- `.gitattributes` pins `eol=lf` so the shebang survives commits from any OS. A
  CRLF line ending on the shebang breaks execution on Linux.
- On Windows/NTFS the config file reads as world-readable, so the "chmod 600
  recommended" warning fires there harmlessly; it is meaningful on the Linux
  deployment.
- uv's managed Pythons do not run on NixOS; run against a nixpkgs Python
  (`uv run --python ...`) there.

## Out of scope (v1)

Multi-account, InfluxDB/Prometheus outputs, historical backfill, and cost/tariff
sensors are deliberately excluded. A NixOS module is planned but deferred.

## dev_scripts

`dev_scripts/` holds one-off exploration tools, not shipped code. They may
import `glowbridge` as a library to reuse its config loader, auth client and
redacting logger. `dev_scripts/*.json` is gitignored: these tools dump raw
personal consumption data, which must never be committed.
