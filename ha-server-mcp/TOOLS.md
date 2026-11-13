# ha-server-mcp tools: observed behaviour

This page describes what each tool actually does, as observed against a real
NixOS host running ha-server-mcp 1.1.0 (November 2026). [README.md](README.md)
covers the intended design. The examples here are trimmed and use neutral
values; host-specific details belong in that host's own config repo.

Every tool returns a single JSON object. Nothing returns an MCP error for
an expected failure. A refused or failed action comes back as data
(`"started": false, "reason": …` or an `"error"` field), so a client can
always read why.

## Connecting

- One tool call over SSH takes about **1 second** end to end on a LAN or
  tailnet, including the SSH handshake and MCP initialisation.
- sshd ignores whatever command the client asks for and runs the server.
  The server writes one `ha-server-mcp: Processing request of type
  CallToolRequest` line to stderr per call. That's harmless, but some
  clients surface it.
- A client that connects before the host config defines the `claude` user
  fails with "connection closed". It needs a reconnect after the first
  switch; it doesn't pick up the user by itself.

## host_health

No inputs. Each section is probed independently; a failing probe becomes
`{"error": "…"}` in that section only.

```json
{
  "uptime": {"uptime_seconds": 7909033, "load": {"1m": 0.03, "5m": 0.13, "15m": 0.16}, "cpus": 8},
  "memory": {"total_mib": 31990, "available_mib": 26693, "swap_total_mib": 15995,
             "swap_free_mib": 15995, "used_percent": 16.6},
  "disks": [{"path": "/", "total_gib": 115.9, "free_gib": 89.8, "used_percent": 22.5}],
  "failed_units": [],
  "home_assistant_vm": {"name": "haos", "state": "running", "reason": "booted"},
  "generations": {"current": 28, "previous": 27,
                  "recent": [{"generation": 27, "created": "…"}, {"generation": 28, "created": "…"}],
                  "reboot_pending": true}
}
```

- `memory.used_percent` is based on `MemAvailable`, not `MemFree`, so page
  cache doesn't count as used.
- `failed_units` is `systemctl list-units --state=failed`. A unit that is
  failing and retrying (`activating (auto-restart)`) doesn't appear there
  until systemd gives up and marks it `failed`. One unit that failed during
  a switch and recovered on its retry was already absent when host_health
  was next called.
- `home_assistant_vm` is `{"configured": false}` when `vm.name` is unset or
  libvirtd is off.
- `generations.recent` lists the last five generations, oldest first.
  `previous` is the highest generation number *below* `current`. It is not
  "the last one that was running". After rollbacks the two differ (see
  system_rollback).
- `reboot_pending` compares `/run/current-system` with
  `/run/booted-system`. It turns `true` after any switch that changes
  them, for example a systemd or kernel update, and stays `true` until a
  reboot.

## glowbridge_status

No inputs. Returns `service`, the unit's systemd state, and `status_file`,
glowbridge's `status.json` verbatim.

```json
{
  "service": {"unit": "glowbridge.service", "ActiveState": "active", "SubState": "running",
              "NRestarts": "0", "ActiveEnterTimestamp": "…", "Result": "success", "…": "…"},
  "status_file": {"bridge_version": "1.0.1", "consecutive_failures": 0, "last_error": "",
                  "last_success": "…", "next_planned_update": "…", "resources": {"…": "…"}}
}
```

- `service` comes from systemd. For a long-running daemon it is reliable,
  unlike for the oneshot jobs below.
- `status_file` is read by a root helper that refuses a symlink.
  `state.json` (the Glow token) is never read.
- `{"configured": false}` is returned when glowbridge isn't enabled on the
  host.

## service_logs(unit, lines=100)

Returns `{"unit": …, "lines": [journal lines, oldest first]}`.

- **Unit names:** a bare name gets `.service` added (`"glowbridge"` →
  `glowbridge.service`). Other suffixes are kept as given.
- **Allow-list:** a unit that isn't listed (including path-like or
  flag-like strings) returns
  `{"error": "unit is not in the log allow-list", "allowed_units": […]}`
  without running anything. That makes it the way to discover the list.
- **Line count:** `lines` is clamped to between 1 and the host's
  `maxLogLines` (default 500). `0` and negative values give one line, and
  `100000` gives the cap.
- **Watch the size of large reads.** 500 lines of a chatty daemon came to
  about 71 KB, which went over Claude Code's limit for a single tool result.
  The client saved the result to a file instead of showing it. Ask for a
  few hundred lines at most, or lower `maxLogLines`.
- **Line format:** lines are `journalctl -o short-iso`:
  `<ISO time> <host> <ident>[<pid>]: <message>`.
- **Quiet units:** a unit that logs rarely returns its *newest* lines,
  however old those are. Check the timestamps before assuming an entry is
  recent.

## system_upgrade

No inputs. Starts the upgrade job (`nixos-rebuild switch --refresh --flake
<upgrade.flake>`) and returns immediately.

```json
{"started": true, "unit": "ha-server-mcp-upgrade.service",
 "previous_run": {"started_at": "…", "finished_at": "…", "result": "success",
                  "exit_code": "exited", "exit_status": "0"},
 "next": "The upgrade runs in the background …",
 "source": "git+ssh://…?ref=main#host"}
```

- **Refusals:** while the upgrade *or* the rollback job is running, it
  returns `{"started": false, "reason": "<unit> is already running"}` and
  starts nothing.
- **`previous_run`:** this is the record from *before* this start (`null` if
  the job has never finished a run). Compare it with `upgrade_status`'s
  `last_run` to tell the new result from the old one.
- **Timings seen:**
  - about 5 s when nothing changed
  - about 13 s for a small config change (only a few derivations rebuilt)
  - longer when a nixpkgs bump means downloads
- **Generations:**
  - An upgrade that builds the *same* system as the running one creates no
    new generation.
  - One that builds a different system, or the same system as an *older*
    generation after a rollback, creates a new number. For example,
    rolling back from 27 to 24 and then upgrading to a config identical to
    24 produced generation 28, with the same store path as 24.
- **Self-deploys:** the upgrade can deploy changes to ha-server-mcp itself.
  The running MCP session keeps working, and the next connection uses the
  new version.

## upgrade_status(log_lines=60)

Returns `upgrade` and `rollback` (each with `unit`, `ActiveState`, `SubState`,
`last_run` and `log`), plus `generations` (same as in host_health) and `source`.

- **Is a job running?** `ActiveState` is the live answer: `activating` means
  the job is running, and `inactive`/`dead` means it's idle. Poll until it
  leaves `activating`. A `deactivating` state can show briefly while the run
  record is being written.
- **`last_run`:** this is the job's own record of its last *completed* run.
  It is the authoritative result, because systemd forgets a oneshot's
  result once the idle unit is unloaded. It's `null` until the job has
  finished at least once.
- **Missing start time:** the deploy that first adds the recording hooks
  writes a record with an empty `started_at`. That run started under the old
  unit definition. Later runs record both times.
- **Log lines:** `log` is the tail of that job's journal. `log_lines` is
  clamped like `service_logs`, so `0` gives one line.

## system_rollback

No inputs. Starts the rollback job: `nix-env --rollback` on the system profile,
then `switch-to-configuration switch`. It returns at once; poll
`upgrade_status`. The response is shaped like system_upgrade's, without
`source`.

- **It's fast:** about 2 s per step, with no network and no build, because
  the target generation is already on disk.
- **It's relative:** each call moves to the next-lower generation number.
  Three calls went 27 → 26 → 25 → 24, and generations 25–27 stayed on disk
  afterwards.
- **It doesn't mean "last good":** after rolling back and upgrading again,
  `previous` is the highest number below the new current, which may be a
  generation you deliberately left. Check `generations` before rolling
  back. When the bad change is in git, a revert PR plus `system_upgrade` is
  usually the cleaner way back.
- **It diverges from git:** the host runs an older generation while the
  config repo's branch hasn't changed. The next `system_upgrade` returns to
  the branch.
- **Refusals:** like system_upgrade, it refuses while either job is
  running.
- **How far back you can go:** rolling back to a generation from before
  ha-server-mcp was set up removes the `claude` user and ends access.
  Recovery then needs a manual `nixos-rebuild switch --flake …` on the host.

## What was verified where

- **Against a live host:** every tool above, including:
  - allow-list refusals and line clamping
  - start refusals while a job runs
  - three upgrades forward and three rollbacks back
  - a self-deploy of a new server version
- **In CI only ([nix/vm-test.nix](nix/vm-test.nix)):** the SSH and sudo
  boundary:
  - a requested SSH command still gets the MCP server, not a shell
  - sudo grants only the fixed helpers, and they refuse extra arguments
  - `state.json` is unreadable
  - a symlinked `status.json` is refused

  These are deliberately not probed against the live host.
