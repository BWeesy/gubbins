# glowbridge

A single-file bridge that fetches half-hourly UK smart meter readings from
the Glowmarkt/DCC API (the backend behind Hildebrand's Bright app) and
publishes a monotonic cumulative consumption total per meter to MQTT, with
Home Assistant discovery.

glowbridge is an unofficial client and has no affiliation with Hildebrand
Technology. Do not lower the polling defaults to hammer their API; the
defaults exist to keep this project a good citizen of a free service.

## Why this exists

The existing Home Assistant HACS integrations for the Glowmarkt DCC API
share a set of structural problems:

- **No meaningful retry logic.** The Glowmarkt API is slow and routinely
  fails overnight, exactly when the previous day's data lands. One failed
  request costs a whole update; sensors sit `unknown` until something
  reloads them.
- **Fragile statistics.** Publishing daily totals that can be revised
  downward corrupts Home Assistant's `total_increasing` statistics: a
  downward revision is interpreted as a meter reset and double-counts.
- **Synchronised polling.** Every install polls on the same schedule,
  contributing to the nightly load spike that makes the API fail in the
  first place.

glowbridge takes a different shape: a small daemon that owns its own retry
policy, publishes values that are monotonic by construction, and spreads
its load across the install base by default.

## Design decisions

**Cumulative totals, not daily figures.** Each meter is published as a
single ever-increasing kWh total, accumulated internally in integer
watt-hours. There is no daily reset in the payload and therefore no
midnight-boundary logic in the bridge; Home Assistant derives per-day
figures in the user's timezone (the Energy dashboard does this natively,
or use a `utility_meter` helper).

**Finalisation lag.** Only intervals older than `schedule.finalisation_lag`
(default 90 minutes) are published. The DCC can revise freshly delivered
data; publishing only settled intervals is what makes the cumulative total
safe for `state_class: total_increasing`. Later revisions to already
published intervals are not tracked.

**Gaps halt, never skip.** Intervals are folded into the total strictly in
order. If a half-hour is missing from the API response, the watermark
holds there — even when later intervals are present — because advancing
past a gap would exclude that half-hour from the total forever. Late
deliveries are picked up by a subsequent cycle and the total self-heals.

**The state file is the single source of truth.** The watermark (how far
the data is complete), the cumulative total, the auth token and the
discovered resource cache live in one JSON file, written atomically,
before anything is published. A crash between write and publish produces
a gap, never a double-count. The retained MQTT status topics carry a copy
of this state for observability only: the bridge subscribes to nothing
and never recovers state from the broker. Contributions adding
recover-from-broker logic will be declined; a stale retained watermark
silently skipping data is precisely the failure class this design
removes.

**Deterministic schedule jitter.** Each install polls at a fixed offset
past the interval boundary, derived from the machine ID. A given install
is predictable (debuggable); the population is spread across the jitter
window (no thundering herd). Cycles are anchored to epoch interval
boundaries, so restarts do not drift the schedule.

**Auth floor.** Authentication attempts are separated by at least
`retry.auth_floor` seconds, persisted across restarts. A crash loop
cannot become a re-auth loop; Hildebrand have locked accounts over
aggressive re-authentication.

**Failures are visible, not silent.** The bridge distinguishes three
conditions that other integrations blur together, via retained status
topics and an MQTT Last Will:

| Condition | Signal |
|---|---|
| Bridge process dead | `glow/bridge/availability` → `offline` (LWT) |
| Bridge alive, API failing | `consecutive_failures` > 0, `next_planned_update` pushed out |
| Bridge and API fine, DCC feed stale | `last_success` recent but `data_complete_to` old |

That last distinction — bridge broken versus DCC constipated — is the one
that matters when triaging missing data.

**Secrets never reach logs.** All log output passes through a redacting
formatter that scrubs the configured secrets and masks token/password
fields in dumped payloads, including at debug level. Debug logs are safe
to paste into an issue.

## What it does not do

- **Cost and tariff sensors.** Cost is derivable in Home Assistant from
  consumption plus a tariff; the API's tariff data is unreliable and
  parsing it has historically broken entire integrations.
- **Real-time data.** The DCC pipeline is half-hourly at best and often
  slower. For ~10-second electricity readings, buy a Glow CAD and point
  it at the same broker; glowbridge then serves as backfill/cross-check.
- **Multiple Bright accounts.** Run one instance per account.

## Requirements

- [uv](https://docs.astral.sh/uv/) (the script carries its own dependency
  metadata; there is nothing to install beyond uv itself)
- A Bright account with your smart meter attached and data visible in the
  app — confirm this first; glowbridge cannot fix an empty account
- An MQTT broker, and Home Assistant with the MQTT integration if
  discovery is wanted

## Quick start

```sh
mkdir -p ~/.config/glowbridge
cp glowbridge.example.toml ~/.config/glowbridge/glowbridge.toml
chmod 600 ~/.config/glowbridge/glowbridge.toml
$EDITOR ~/.config/glowbridge/glowbridge.toml   # credentials, mqtt.host

./glowbridge.py --dry-run   # fetch and print, no publish, no state writes
./glowbridge.py --once      # one real cycle, then exit
./glowbridge.py             # run as a daemon
```

On first run the bridge discovers your consumption resources, seeds each
watermark 24 hours before the current time, and publishes a cumulative
total covering that first day of catch-up. Home Assistant discovery
entities appear after the first successful cycle; add the consumption
sensors to the Energy dashboard as grid consumption. Statistics accrue
from that point forward — historical backfill beyond the initial 24 hours
is deliberately out of scope.

## MQTT topics

| Topic | Retained | Payload |
|---|---|---|
| `glow/{resource_id}/state` | no | cumulative kWh, 3 decimal places |
| `glow/{resource_id}/status` | yes | JSON: `data_complete_to`, `cumulative_kwh`, `last_success`, `next_planned_update`, `consecutive_failures`, `bridge_version` |
| `glow/bridge/availability` | yes | `online` / `offline` (Last Will) |
| `glow/bridge/status` | yes | JSON: bridge-level summary |
| `homeassistant/sensor/glowbridge_{id}/config` | yes | HA discovery |

`glow` is `mqtt.topic_prefix`. The per-resource status topic doubles as
the sensor's `json_attributes_topic`, so `data_complete_to` and friends
are visible as entity attributes in Home Assistant without extra
configuration.

## Troubleshooting

Start with one question: **is it the bridge, or is it the DCC?**

```sh
mosquitto_sub -v -t 'glow/#'
```

- `availability` is `offline` → the process is dead. Check the service.
- `consecutive_failures` climbing → the bridge cannot complete a cycle.
  Run `./glowbridge.py --once --debug` and read the log; it is safe to
  paste.
- `last_success` is recent but `data_complete_to` is hours behind → the
  bridge is fine and the DCC pipeline is behind. This is common,
  especially before the previous day is finalised overnight. Check the
  Bright app: if the data is missing there too, nothing downstream can
  conjure it. Persistent daily-only granularity usually means half-hourly
  consent has lapsed with your supplier.
- Empty discovery / "no consumption resources" → the Bright account has
  no meters attached, or the DCC enrolment has not completed. Fix in the
  Bright app first.

## Development

```sh
uv run test_glowbridge.py     # or: pytest test_glowbridge.py
ruff check glowbridge.py test_glowbridge.py
```

The test suite concentrates on the places the subtle bugs live: DST
transition days (23- and 25-hour days), gap handling, emission
monotonicity, config validation and crash-ordering. There are no live-API
tests and none should be added.

Two things are treated as ABI once released: the MQTT topic layout
(including discovery `unique_id`s — changing them orphans users'
entities) and the state file schema (bump `schema` and migrate, never
reinterpret).
