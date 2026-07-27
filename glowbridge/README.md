# glowbridge

A single-file daemon that fetches hourly UK smart meter readings from the
Glowmarkt/DCC API (the backend behind Hildebrand's Bright app) and imports
them into Home Assistant as long-term statistics, stamped with each hour's
true time so the Energy dashboard shows *when* energy was used.

glowbridge is an unofficial client and has no affiliation with Hildebrand
Technology. Do not lower the polling defaults to hammer their API; the
defaults exist to keep this project a good citizen of a free service.

## Why this exists

The existing Home Assistant HACS integrations for the Glowmarkt DCC API
share a set of structural problems:

- **Consumption lands at the wrong time.** They publish to a sensor and let
  Home Assistant record it at arrival time. Because the DCC delivers with
  hours of delay, the Energy dashboard's bars end up shifted — and a
  catch-up after downtime dumps a whole day into a single hour. The DCC
  tells you exactly when each interval happened; that information gets
  thrown away.
- **No meaningful retry logic.** The API is slow and routinely fails
  overnight, exactly when the previous day's data lands. One failed request
  costs a whole update.
- **Synchronised polling.** Every install polls on the same schedule,
  contributing to the nightly load spike that makes the API fail.

glowbridge takes a different shape. It imports into Home Assistant's
**long-term statistics** via the `recorder/import_statistics` WebSocket
command, which places each hour at its true timestamp regardless of when
the import happens. That fixes the timing, lets it **backfill a year of
history on first run**, owns its own retry policy, and spreads its load
across the install base by default.

## Design decisions

**Correct time, via statistics import.** Each hour of consumption is
imported with its real `start`, so it appears in the hour it actually
happened — even if imported days late. This is the whole point: an Energy
dashboard is only useful if the shape of the day is right.

**Hourly, because that is HA's ceiling.** Home Assistant long-term
statistics are hourly buckets, so glowbridge fetches the API at hourly
resolution (`period=PT1H`) and imports hourly rows. Sub-hourly history is
not something the Energy dashboard can hold; it is out of scope.

**We supply the cumulative sum.** A statistic with a sum stores a running
total, and Home Assistant derives each hour's consumption by differencing
consecutive rows. glowbridge keeps a synthetic monotonic total per meter
(integer watt-hours, seeded at zero) and emits, for each hour, the sum
through the end of that hour. Its absolute value is meaningless; only the
deltas matter.

**Finalisation lag.** An hour is not imported until its end is at least
`schedule.finalisation_lag` (default 90 minutes) in the past, keeping the
freshest, most-volatile hour out until it settles.

**Self-healing by re-import, not bookkeeping.** Every cycle re-fetches and
re-imports a trailing `schedule.revision_window` (default 7 days). Imports
are idempotent — keyed by hour — so a DCC revision or a gap the DCC
backfills days late simply overwrites the affected hours next cycle. A
missing hour is skipped entirely (no fabricated zero) and fills in when it
arrives. Nothing stalls; the only data lost is a backfill that arrives
*later* than the revision window.

**The state file is the single source of truth.** A settled frontier
(`settled_through`), the cumulative baseline at that point, `data_complete_to`
and the discovered resource cache live in one JSON file, written
atomically. glowbridge never reads statistics back from Home Assistant — HA
is a write-only sink. Each cycle imports first, then advances the frontier,
so a crash re-imports (idempotent) rather than skipping data. Contributions
adding recover-from-HA logic will be declined.

**Deterministic schedule jitter.** Each install polls at a fixed offset
past the interval boundary, derived from the machine ID: predictable
locally, decorrelated across the population (no thundering herd). Cycles are
anchored to epoch interval boundaries, so restarts do not drift the
schedule.

**Auth floor.** Glow authentication attempts are separated by at least
`retry.auth_floor` seconds, persisted across restarts. A crash loop cannot
become a re-auth loop; Hildebrand have locked accounts over aggressive
re-authentication.

**Conditional catchup.** The DCC `catchup` endpoint is nudged only when a
meter is stale by more than `schedule.catchup_stale_after` (default a day),
throttled to once per 30 minutes and persisted across restarts. When the
feed is keeping pace there is nothing to nudge; firing it only on genuine
staleness keeps glowbridge a good citizen.

**Failures are visible, not silent.** glowbridge writes a `status.json`
next to the state file every cycle, distinguishing three conditions:

| Condition | Signal |
|---|---|
| Process dead | the systemd unit is not active; the status file goes stale |
| Bridge alive, API failing | `consecutive_failures` > 0, `last_error` set |
| Bridge and API fine, DCC feed stale | `last_success` recent but `data_complete_to` old |

That last distinction — bridge broken versus DCC constipated — is the one
that matters when triaging missing data.

**Secrets never reach logs.** All log output passes through a redacting
formatter that scrubs the configured Bright password and HA token and masks
token/password fields in dumped payloads, including at debug level. Both
debug logs and `status.json` are safe to paste into an issue; the auth
token lives only in `state.json`, which is not.

## What it does not do

- **Cost and tariff sensors.** Cost is derivable in Home Assistant from
  consumption plus a tariff; the API's tariff data is unreliable and
  parsing it has historically broken entire integrations.
- **Real-time or sub-hourly data.** The DCC pipeline is half-hourly at best
  and often slower, and HA statistics are hourly regardless. For ~10-second
  electricity readings, buy a Glow CAD; glowbridge is the long-term,
  correctly-timed record, not a live feed.
- **Multiple Bright accounts.** Run one instance per account.

## Requirements

- [uv](https://docs.astral.sh/uv/) (the script carries its own dependency
  metadata; there is nothing to install beyond uv itself)
- A Bright account with your smart meter attached and data visible in the
  app — confirm this first; glowbridge cannot fix an empty account
- A Home Assistant instance reachable over WebSocket, and a long-lived
  access token (HA profile → Security → Long-lived access tokens). No
  message broker or other intermediate service is needed.

## Quick start

```sh
mkdir -p ~/.config/glowbridge
cp glowbridge.example.toml ~/.config/glowbridge/glowbridge.toml
chmod 600 ~/.config/glowbridge/glowbridge.toml
$EDITOR ~/.config/glowbridge/glowbridge.toml   # credentials, homeassistant.url

export GLOWBRIDGE_HA_TOKEN=...   # or put token = "..." in the config

./glowbridge.py --dry-run   # fetch and print what would be imported; no HA, no state
./glowbridge.py --once      # one real cycle, then exit
./glowbridge.py             # run as a daemon
```

On first run the bridge discovers your consumption meters and backfills
`schedule.backfill_lookback` (default a year) of hourly history, then keeps
up in real time. In Home Assistant, open the Energy dashboard and add the
`glowbridge:electricity_consumption` (and `:gas_consumption`) statistics as
grid consumption — they appear in the picker automatically once the first
import completes; there is no device or entity to configure. A year of
correctly-timed history is there from the start.

## Home Assistant statistics

glowbridge creates external statistics — no entities or devices to configure:

| Statistic ID | Unit | Content |
|---|---|---|
| `glowbridge:electricity_consumption` | kWh | hourly grid electricity |
| `glowbridge:gas_consumption` | kWh | hourly gas |

Each hour's row carries a `start` (the hour's beginning) and a `sum` (the
cumulative total through that hour's end); Home Assistant differences
consecutive sums to fill the Energy dashboard. Re-importing an hour
overwrites it, which is how revisions and late gap-fills correct themselves.

## Observability: `status.json`

Written next to `state.json` every cycle, safe to read for triage (no
secrets):

```json
{
  "bridge_version": "1.0.0",
  "last_attempt": "...", "last_success": "...",
  "consecutive_failures": 0, "next_planned_update": "...",
  "last_error": "",
  "resources": {
    "glowbridge:electricity_consumption": {
      "settled_through": "...", "data_complete_to": "...",
      "cumulative_kwh": 1234.5, "last_catchup_at": ""
    }
  }
}
```

Because Home Assistant runs elsewhere (often a separate VM), this is a
host-side debugging artifact — read it over SSH alongside `journalctl`, not
via an HA sensor.

## Troubleshooting

Start with one question: **is it the bridge, or is it the DCC?**

```sh
systemctl status glowbridge          # is the process alive?
cat "$STATE_DIRECTORY/status.json"   # or the resolved state dir
```

- Unit dead / status file stale → the process is down. Check the service.
- `consecutive_failures` climbing, `last_error` set → the bridge cannot
  complete a cycle. Run `./glowbridge.py --once --debug` and read the log;
  it is redaction-safe to paste.
- `last_success` recent but `data_complete_to` hours behind → the bridge is
  fine and the DCC pipeline is behind. Common, especially before the
  previous day finalises overnight. Check the Bright app: if the data is
  missing there too, nothing downstream can conjure it. Persistent
  daily-only granularity usually means half-hourly consent has lapsed with
  your supplier.
- "discovery found no consumption resources" → the Bright account has no
  meters attached, or DCC enrolment has not completed. Fix in the Bright
  app first.
- Import fails with an auth error → the HA long-lived token is missing,
  wrong, or expired. `--dry-run`/`--dump-raw` work without it; a real run
  needs it in `GLOWBRIDGE_HA_TOKEN` or the config.

`./glowbridge.py --dump-raw` writes the raw, untransformed API rows (nulls
included) to JSON for inspecting exactly what the DCC returned.

## Development

```sh
uv run --locked test_glowbridge.py    # tests; --locked also catches lock drift
uvx ruff check glowbridge.py test_glowbridge.py

# Dependencies are pinned in glowbridge.py.lock / test_glowbridge.py.lock.
# Re-lock after changing the PEP 723 metadata, or --locked runs will fail:
uv lock --script glowbridge.py

# Audit the pinned set for known advisories:
uv export --script glowbridge.py --format requirements-txt > /tmp/req.txt
uvx pip-audit -r /tmp/req.txt
```

CI (`.github/workflows/glowbridge.yml`) runs all of the above on every push
touching `glowbridge/`, and re-runs the audit weekly — pinned dependencies
never change, but advisories against them do.

The test suite concentrates on where the subtle bugs live: the hourly
cumulative walk (`plan_import` — sum-through-end-of-hour, gaps, finalisation,
frontier advance), revision re-import across cycles, the WebSocket
auth+import handshake (via a fake socket), conditional/throttled catchup,
import-then-commit crash ordering, schema fresh-start, and redaction. The
Glowmarkt API and the Home Assistant WebSocket are exercised through fakes;
there are no live-API tests and none should be added.

Two things are treated as ABI once history exists behind them: the
statistic IDs (`glowbridge:electricity_consumption` /
`glowbridge:gas_consumption` — renaming orphans a meter's history) and the
state-file `schema` (bump it on a format change; an unrecognised schema is
treated as a fresh install, never reinterpreted).
