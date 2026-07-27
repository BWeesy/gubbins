# Configuration reference

glowbridge reads a single TOML file. Default path:
`$XDG_CONFIG_HOME/glowbridge/glowbridge.toml`, falling back to
`~/.config/glowbridge/glowbridge.toml`; override with `--config PATH`.

Validation is strict. Any key not listed in this document, in any table,
is a fatal startup error, as is any value of the wrong type. Booleans are
not accepted where integers are expected. All durations are integer
seconds; there are no duration strings.

If the file stores credentials inline and is group- or world-readable, a
warning is logged at startup recommending `chmod 600`. The warning is
suppressed when the file contains no secrets (credentials supplied via
environment).

## Environment variables

Environment variables carry secrets only and take precedence over the
file. No other setting can be set from the environment.

| Variable | Overrides |
|---|---|
| `GLOWBRIDGE_GLOW_USERNAME` | `glowmarkt.username` |
| `GLOWBRIDGE_GLOW_PASSWORD` | `glowmarkt.password` |
| `GLOWBRIDGE_HA_TOKEN` | `homeassistant.token` |

## `[glowmarkt]`

### `username` — string, required
Bright account email address. Required after environment merge: it may be
empty in the file if `GLOWBRIDGE_GLOW_USERNAME` is set.

### `password` — string, required
Bright account password. Same environment-merge rule via
`GLOWBRIDGE_GLOW_PASSWORD`. Included in the log redaction set.

### `application_id` — string, default `"b0f1b774-a586-4f72-9edd-27ead8aa7a8d"`
Sent as the `applicationId` header on every API request. The default is
the published Bright application ID. Must be non-empty. Exists so that a
rotation by Hildebrand is a config change, not a release.

### `resources` — array of strings, default `[]` (unset)
Optional resource pin. Semantics:

- **Unset (default):** on first run the bridge calls `GET /resource`,
  keeps entries whose classifier is `electricity.consumption` or
  `gas.consumption` (cost resources are never bridged), and caches them
  in the state file. Subsequent runs use the cache. If a cached resource
  later returns HTTP 404 — supplier switches and meter exchanges mint new
  resource IDs — it is dropped and discovery re-runs automatically; the
  replacement is polled from the next cycle.
- **Set:** discovery is skipped entirely. A pinned resource that returns
  404 is a fatal cycle error, deliberately: the ID was asserted by the
  user, and silently substituting a discovered one would be wrong.

Entries must be non-empty strings. A pinned resource's classifier is never
looked up, so its statistic is named after the resource id
(`glowbridge:<resource_id>`) rather than the classifier.

## `[homeassistant]`

### `url` — string, required
WebSocket URL of the Home Assistant instance, e.g.
`ws://homeassistant.local:8123/api/websocket`. Must begin with `ws://` or
`wss://`. Plain `ws://` is correct over a trusted path — a Tailscale/mesh
VPN link or localhost is already encrypted — so TLS on top is redundant and
HAOS serves `:8123` in plaintext by default. Use `wss://` when the path to
HA crosses an untrusted network.

### `token` — string, required (to import)
A Home Assistant long-lived access token (HA profile → Security →
Long-lived access tokens → Create Token). Same environment-merge rule via
`GLOWBRIDGE_HA_TOKEN` (env wins); included in the log redaction set. It is
**not** validated at config load, so `--dry-run` and `--dump-raw` run
without it; a missing token surfaces when the importer connects.

glowbridge imports into external statistics named
`glowbridge:electricity_consumption` and `glowbridge:gas_consumption`.
These appear in the Energy dashboard's consumption picker; no entity or
device setup is needed — the first import creates them.

## `[schedule]`

### `interval` — integer seconds, default `1800`, minimum `1800`
Target spacing between poll cycles. Cycles are anchored to epoch multiples
of `interval` (plus the install offset), not to process start time, so
restarts do not drift the schedule. The DCC is half-hourly at best, so
polling faster than 30 minutes buys nothing except load on a free API; the
minimum is enforced.

### `jitter` — integer seconds, default `300`, minimum `0`
Width of the per-install schedule offset window. The actual offset is
deterministic: `sha256(machine-id) mod (jitter + 1)`, computed from
`/etc/machine-id` (falling back to `/var/lib/dbus/machine-id`, then the
hostname). A given machine always polls at the same second past the
boundary; different machines are spread uniformly across the window. `0`
disables the offset entirely. The offset is logged at startup and reflected
in the status file's `next_planned_update`.

### `finalisation_lag` — integer seconds, default `5400`, minimum `0`
An hour is imported only once its end time is at least this far in the
past, keeping the freshest, most-volatile hour out until it settles. Unlike
older designs this is not a correctness guard — a later revision is simply
re-imported (see `revision_window`) — but it avoids churning on data the
DCC is still finalising. The default is 90 minutes. `0` imports right up to
the current hour boundary.

### `backfill_lookback` — integer seconds, default `31536000` (365 days), minimum `0`
On a fresh install (no prior state) each meter's frontier is seeded this
far in the past, so the first run backfills that much history — a year of
correctly-timed hourly consumption on the Energy dashboard from day one.
Ignored once state exists (the stored frontier rules). Because imports are
idempotent, losing the state file and re-seeding just re-imports the same
window harmlessly.

### `revision_window` — integer seconds, default `604800` (7 days), must be >= `finalisation_lag`
The trailing span re-fetched and re-imported every cycle. The DCC revises
recently delivered data and backfills gaps days late; re-importing this
window each cycle lets those corrections overwrite the affected hours
(imports are idempotent, keyed by hour). Data older than the window is
never re-fetched — the frontier has settled past it — so a DCC backfill
arriving *later* than `revision_window` is permanently missed. Observed DCC
backfills run to ~5 days, hence the 7-day default; re-importing ~168 hourly
rows per meter per cycle is negligible. It must cover at least
`finalisation_lag`, or a revision to a just-finalised hour would never be
re-checked.

### `catchup_stale_after` — integer seconds, default `86400` (1 day), minimum `0`
How long a meter may go without new data before glowbridge nudges the DCC
via the `catchup` endpoint. When `now - data_complete_to` exceeds this, and
no catchup has fired in the last 30 minutes (the API's documented limit,
persisted so a restart cannot exceed it), one best-effort nudge is sent.
The default sits well above the DCC's normal few-hours delay, so it fires
only on genuinely abnormal staleness. catchup is best effort — it times out
routinely and never fails a cycle.

## `[retry]`

Retries operate within a cycle. A cycle that exhausts its budget is
skipped — the daemon logs it, writes failure status, and waits for the next
scheduled cycle. Cycle failure is never fatal to the daemon.

### `max_attempts` — integer, default `5`, minimum `1`
Attempts per cycle before it is abandoned.

### `backoff_base` — integer seconds, default `60`, minimum `1`
### `backoff_max` — integer seconds, default `1800`, minimum `backoff_base`
Delay before attempt *n* is `backoff_base * 2^(n-1)`, capped at
`backoff_max`, then replaced by a uniformly random value between 1 and
that cap ("full jitter"), so installs that failed on the same upstream
incident retry decorrelated. An HTTP 429 with a `Retry-After` header
overrides the computed delay with the server's value.

### `auth_floor` — integer seconds, default `3600`, minimum `0`
Minimum time between Glow authentication attempts, enforced regardless of
outcome and persisted in the state file, so it holds across restarts and
crash loops. When a cycle needs a token and the floor is not met, the cycle
fails immediately (no in-cycle retry can help) and auth is retried no
earlier than the floor allows. Within a cycle, at most one
re-authentication is attempted in response to a rejected token. Raise this
if Hildebrand rate-limit the account; lowering it below 600 invites account
lockout. (This governs the Glow API token only; the HA token is a
long-lived token supplied via config/env.)

## `[state]`

### `dir` — string path, default `""`
Directory holding `state.json` (the per-resource settled frontier,
cumulative baseline in integer watt-hours, `data_complete_to`, catchup
timestamp, cached Glow auth token and discovered resource cache) and
`status.json` (see the README). Resolution when empty, in order:

1. `$STATE_DIRECTORY` (set by systemd for units declaring
   `StateDirectory=`; the first entry if multiple)
2. `$XDG_STATE_HOME/glowbridge`
3. `~/.local/state/glowbridge`

`state.json` is written atomically (temp file, fsync, rename) with mode
`0600`, since it contains the Glow API token. It carries a `schema`
version; a missing, unreadable, corrupt, or unrecognised-schema file is
logged and treated as a fresh install, which reseeds the frontier
`backfill_lookback` in the past and re-runs the backfill. Because imports
are idempotent this re-imports the same hours harmlessly rather than
crash-looping. `status.json` is observability only — it is never read back.

## `[logging]`

### `level` — string, default `"info"`, accepted `debug`, `info`, `warning`, `error`
`--debug` on the command line forces `debug` regardless of this setting.
At `debug`, raw API interactions are logged; redaction (below) still
applies, so debug output is safe to attach to an issue.

### `format` — string, default `"text"`, accepted `text` or `json`
`text` is `timestamp level logger: message` on stderr. `json` emits one
object per line (`ts`, `level`, `logger`, `msg`) for log shippers.

Regardless of level and format, every log line is scrubbed: the configured
Bright password and HA token are replaced with `***` wherever they appear,
and `"token"`/`"password"`/`"access_token"` fields inside logged JSON
payloads are masked. The `urllib3` and `websocket` loggers are held at INFO
even under `--debug` because their payload logging bypasses this
formatter's secret list.
