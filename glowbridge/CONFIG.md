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
| `GLOWBRIDGE_MQTT_PASSWORD` | `mqtt.password` |

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

Entries must be non-empty strings. Pinned resources are polled even if
they are not consumption classifiers; the pin is trusted verbatim.

## `[schedule]`

### `interval` — integer seconds, default `1800`, minimum `300`
Target spacing between poll cycles. Cycles are anchored to epoch
multiples of `interval` (plus the install offset), not to
process start time, so restarts do not drift the schedule. The DCC
delivers half-hourly data at best; polling faster than the default buys
nothing except load on a free API. The minimum is enforced.

### `jitter` — integer seconds, default `300`, minimum `0`
Width of the per-install schedule offset window. The actual offset is
deterministic: `sha256(machine-id) mod (jitter + 1)`, computed from
`/etc/machine-id` (falling back to `/var/lib/dbus/machine-id`, then the
hostname). A given machine always polls at the same second past the
boundary; different machines are spread uniformly across the window.
`0` disables the offset entirely. The offset is logged at startup and
reflected in `next_planned_update`.

### `finalisation_lag` — integer seconds, default `5400`, minimum `0`
An interval is published only once its end time is at least this far in
the past. Freshly delivered DCC data can be revised; once an interval is
folded into the cumulative total it is never revisited, so this lag is
the revision guard. The default (90 minutes) covers typical revision
behaviour. Raising it delays data; lowering it below ~3600 risks
publishing values the DCC subsequently changes, which the bridge will
not correct. `0` is permitted for testing against fixtures and is not
suitable for live use.

### `heal_horizon` — integer seconds, default `604800` (7 days), must exceed `finalisation_lag`
How long a missing half-hour is tolerated before the completeness frontier
gives up on it.

Emission tracks a contiguous frontier (the watermark, published as
`data_complete_to`) plus a ledger of windows already emitted *ahead* of it.
When the DCC is missing a half-hour, later windows are still emitted
immediately and recorded in the ledger — consumption is never withheld —
but the frontier holds at the gap, because everything up to the frontier is
what is provably complete. If the missing window later arrives it is
emitted exactly once and the frontier sweeps forward, draining the ledger.

`heal_horizon` bounds that wait. A gap older than this is abandoned: the
frontier steps over it, permanently losing that one half-hour from the
cumulative total, so a window the DCC never delivers cannot freeze
`data_complete_to` forever (and the ledger stays bounded to roughly this
span). It must be larger than `finalisation_lag` — a window has to outlive
its revision guard before it can be written off. Raise it to give a
chronically slow feed more time to backfill at the cost of `data_complete_to`
lagging longer before it gives up; lower it to advance the completeness
signal sooner at the cost of writing off recoverable gaps.

## `[retry]`

Retries operate within a cycle. A cycle that exhausts its budget is
skipped — the daemon logs it, publishes failure status, and waits for the
next scheduled cycle. Cycle failure is never fatal to the daemon.

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
Minimum time between authentication attempts, enforced regardless of
outcome and persisted in the state file, so it holds across restarts and
crash loops. When a cycle needs a token and the floor is not met, the
cycle fails immediately (no in-cycle retry can help) and auth is retried
no earlier than the floor allows. Within a cycle, at most one
re-authentication is attempted in response to a rejected token. Raise
this if Hildebrand rate-limit the account; lowering it below 600 invites
account lockout.

## `[mqtt]`

### `host` — string, default `"localhost"`, must be non-empty
### `port` — integer, default `1883`, range 1–65535
Broker address. The port is not switched automatically when TLS is
enabled; set `8883` (or your broker's TLS port) explicitly.

### `username` — string, default `""`
### `password` — string, default `""`
Broker credentials; both empty means anonymous. The password can come
from `GLOWBRIDGE_MQTT_PASSWORD` and is included in the log redaction set.

### `client_id` — string, default `"glowbridge"`
MQTT client identifier. Must be unique per broker; change it when running
multiple instances (e.g. one per Bright account) against one broker.

### `topic_prefix` — string, default `"glow"`
Root of the topic layout: `{prefix}/{resource_id}/state`,
`{prefix}/{resource_id}/status`, `{prefix}/bridge/availability`,
`{prefix}/bridge/status`. Must be non-empty and contain no `#` or `+`.
Changing it after Home Assistant has discovered the entities requires the
retained discovery configs to be republished, which happens on the next
successful first cycle after restart.

### `discovery_prefix` — string, default `"homeassistant"`
Prefix for Home Assistant MQTT discovery config topics. Must match the
`discovery_prefix` configured in HA's MQTT integration (default
`homeassistant`).

### `qos` — integer, default `1`, accepted `0` or `1`
QoS for every publish, including discovery and Last Will. At `1`, each
publish waits up to 10 seconds for broker acknowledgement and an
unacknowledged publish fails the cycle (state is already persisted; the
publish is retried by the next cycle as part of normal catch-up). At `0`,
publishes are fire-and-forget and broker loss is only visible via the
Last Will. QoS 2 is not supported.

## `[mqtt.tls]`

### `enabled` — boolean, default `false`
Enables TLS on the broker connection. Setting any other key in this table
while `enabled = false` is a config error, so a half-configured TLS
section fails fast instead of silently connecting in plaintext.

### `ca_cert` — string path, default `""`
PEM CA bundle used to verify the broker. Empty uses the system trust
store — correct for brokers with certificates from a public CA; set a
path for a private CA.

### `client_cert` / `client_key` — string paths, default `""`
Client certificate and key for mutual TLS. Must be set together; setting
one without the other is a config error.

### `insecure_skip_verify` — boolean, default `false`
Disables broker certificate verification while keeping encryption.
Deliberately ugly name. Lab use only: with verification off, anyone on
the path can impersonate the broker and read half-hourly occupancy data.

## `[state]`

### `dir` — string path, default `""`
Directory holding `state.json` — per-resource frontier watermark and
emitted-window ledger, cumulative totals (integer watt-hours), the cached
auth token and the discovered resource cache. Resolution when empty, in
order:

1. `$STATE_DIRECTORY` (set by systemd for units declaring
   `StateDirectory=`; the first entry if multiple)
2. `$XDG_STATE_HOME/glowbridge`
3. `~/.local/state/glowbridge`

The file is written atomically (temp file, fsync, rename) with mode
`0600`, since it contains the API token. It carries a `schema` version and
is migrated forward in place on upgrade (preserving cumulative totals, so
an upgrade emits no spurious meter-reset); an *unrecognised* schema, or a
missing, unreadable or corrupt file, is logged and treated as a fresh
install: watermarks reseed 24 hours before the current run, the cumulative
total restarts, and Home Assistant records a single meter-reset event.
Nothing crash-loops on bad state.

## `[logging]`

### `level` — string, default `"info"`, accepted `debug`, `info`, `warning`, `error`
`--debug` on the command line forces `debug` regardless of this setting.
At `debug`, raw API interactions are logged; redaction (below) still
applies, so debug output is safe to attach to an issue.

### `format` — string, default `"text"`, accepted `text` or `json`
`text` is `timestamp level logger: message` on stderr. `json` emits one
object per line (`ts`, `level`, `logger`, `msg`) for log shippers.

Regardless of level and format, every log line is scrubbed: the
configured Bright and MQTT passwords are replaced with `***` wherever
they appear, and `"token"`/`"password"` fields inside logged JSON
payloads are masked. The `paho` and `urllib3` loggers are held at INFO
even under `--debug` because their payload logging bypasses this
formatter's secret list.
