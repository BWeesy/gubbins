#!/usr/bin/env -S uv run --script
# SPDX-License-Identifier: MIT
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     # Held below 2: nixpkgs ships the 1.x line, and the Nix package must
#     # run the same FastMCP API that the tests and the lockfile exercise.
#     "mcp>=1.29,<2",
# ]
# ///
"""ha-server-mcp: a narrowly scoped MCP server for a NixOS home server.

Lets an AI assistant check the host's health, read glowbridge's status and
an allow-list of service logs, and trigger a NixOS upgrade or rollback —
nothing else. It speaks MCP over stdio and is launched by sshd as the
`command=` of a single restricted authorized_keys entry; see nix/module.nix.

Design invariants — do not break these when modifying:

  * There is NO generic "run a command" tool, and no tool takes a command,
    path, flake ref, flag or unit name that reaches a privileged process.
    Every privileged action is a fixed, argument-less helper script that
    sudo allows by exact store path (with "" so no arguments are accepted).
    Caller input only ever selects between helpers the config already names.
  * stdio only. No network listener, no HTTP, no tokens: SSH is the
    authentication, and the forced command means the client cannot choose
    what runs. SSH_ORIGINAL_COMMAND is deliberately ignored.
  * glowbridge's state.json (which holds the Glow auth token) is never read.
    The only file read under /var/lib/glowbridge is status.json, by a helper
    that refuses to follow a symlink at that path.
  * stdout is the MCP protocol stream. Logging goes to stderr, and every
    child process gets stdin=/dev/null so it cannot eat protocol bytes.
  * Read-only tools degrade per section: one failing probe (no virsh, a
    sudo refusal) is reported in its section, never fails the whole call.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "1.1.1"

log = logging.getLogger("ha-server-mcp")

# Cap on any child's captured stdout, so a runaway journal cannot balloon a
# tool response (the helpers already cap their own output; this is a
# backstop).
MAX_OUTPUT_BYTES = 512 * 1024
COMMAND_TIMEOUT = 30  # seconds; every probe here is a quick read

# systemctl show properties surfaced for a unit. Kept to state and timing:
# nothing here can carry a secret (no Environment=, no ExecStart= argv).
UNIT_PROPERTIES = (
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainStatus",
    "NRestarts",
    "ActiveEnterTimestamp",
    "InactiveEnterTimestamp",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
)

# A unit is busy while systemd is starting, running or stopping it. For a
# oneshot, "activating" is the whole time the job runs.
BUSY_STATES = frozenset({"activating", "active", "deactivating", "reloading"})

GENERATION_LINK = re.compile(r"^system-(\d+)-link$")

# The parts of a NixOS system a switch can't apply to the running host:
# they only take effect on boot. The same three that nixpkgs'
# system.autoUpgrade.allowReboot compares. Anything else (units, /etc,
# packages) is live after a switch, so comparing whole systems would flag a
# reboot after nearly every deploy.
BOOT_COMPONENTS = ("kernel", "initrd", "kernel-modules")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class GlowbridgeConfig:
    unit: str
    status_helper: str


@dataclass(frozen=True)
class VmConfig:
    name: str
    helper: str


@dataclass(frozen=True)
class JobConfig:
    unit: str
    start_helper: str
    # JSON record of the job's last completed run, written by the unit
    # itself (see nix/module.nix). systemd forgets a oneshot's result and
    # timestamps once it unloads the idle unit, so this is the durable copy.
    last_run_file: str | None = None


@dataclass(frozen=True)
class Config:
    sudo: str
    systemctl: str
    upgrade: JobConfig
    rollback: JobConfig
    # unit name -> argument-less helper that prints that unit's journal tail
    log_units: dict[str, str]
    max_log_lines: int = 500
    disk_paths: tuple[str, ...] = ("/",)
    profiles_dir: str = "/nix/var/nix/profiles"
    glowbridge: GlowbridgeConfig | None = None
    vm: VmConfig | None = None
    upgrade_source: str = ""


def _need(obj: dict, key: str, kind: type, where: str) -> Any:
    value = obj.get(key)
    if not isinstance(value, kind):
        raise ConfigError(f"{where}{key} must be a {kind.__name__}")
    return value


def _job(raw: dict, key: str) -> JobConfig:
    job = _need(raw, key, dict, "")
    last_run = job.get("last_run_file")
    if last_run is not None and not isinstance(last_run, str):
        raise ConfigError(f"{key}.last_run_file must be a str")
    return JobConfig(
        unit=_need(job, "unit", str, f"{key}."),
        start_helper=_need(job, "start_helper", str, f"{key}."),
        last_run_file=last_run,
    )


def parse_config(raw: Any) -> Config:
    """Build a Config from the JSON the NixOS module renders. Strict: an
    unexpected shape is a ConfigError at startup, not a surprise mid-call."""
    if not isinstance(raw, dict):
        raise ConfigError("top level must be an object")
    log_units = _need(raw, "log_units", dict, "")
    for unit, helper in log_units.items():
        if not isinstance(helper, str):
            raise ConfigError(f"log_units.{unit} must be a helper path")

    glowbridge = None
    if raw.get("glowbridge") is not None:
        gb = _need(raw, "glowbridge", dict, "")
        glowbridge = GlowbridgeConfig(
            unit=_need(gb, "unit", str, "glowbridge."),
            status_helper=_need(gb, "status_helper", str, "glowbridge."),
        )

    vm = None
    if raw.get("vm") is not None:
        vmraw = _need(raw, "vm", dict, "")
        vm = VmConfig(
            name=_need(vmraw, "name", str, "vm."),
            helper=_need(vmraw, "helper", str, "vm."),
        )

    max_lines = raw.get("max_log_lines", 500)
    if not isinstance(max_lines, int) or isinstance(max_lines, bool) or max_lines < 1:
        raise ConfigError("max_log_lines must be a positive integer")

    disk_paths = raw.get("disk_paths", ["/"])
    if not isinstance(disk_paths, list) or not all(
        isinstance(p, str) for p in disk_paths
    ):
        raise ConfigError("disk_paths must be a list of strings")

    return Config(
        sudo=_need(raw, "sudo", str, ""),
        systemctl=_need(raw, "systemctl", str, ""),
        upgrade=_job(raw, "upgrade"),
        rollback=_job(raw, "rollback"),
        log_units=dict(log_units),
        max_log_lines=max_lines,
        disk_paths=tuple(disk_paths),
        profiles_dir=raw.get("profiles_dir", "/nix/var/nix/profiles"),
        glowbridge=glowbridge,
        vm=vm,
        upgrade_source=raw.get("upgrade_source", ""),
    )


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return parse_config(raw)


# --------------------------------------------------------------------------
# Host access seams
#
# Everything that touches the machine goes through Runner (child processes)
# or Fs (files and links), so the tests drive the tools with fakes and never
# shell out.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], RunResult]


def subprocess_runner(argv: list[str]) -> RunResult:
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,  # stdin is the MCP stream; never share it
            capture_output=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        return RunResult(127, "", f"{argv[0]}: not found")
    except subprocess.TimeoutExpired:
        return RunResult(124, "", f"{argv[0]}: timed out after {COMMAND_TIMEOUT}s")
    return RunResult(
        proc.returncode,
        proc.stdout[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"),
        proc.stderr[:MAX_OUTPUT_BYTES].decode("utf-8", "replace"),
    )


@dataclass
class Fs:
    """Thin filesystem seam. Defaults are the real calls; tests swap them."""

    read_text: Callable[[str], str] = field(
        default=lambda p: Path(p).read_text(encoding="utf-8")
    )
    listdir: Callable[[str], list[str]] = field(default=os.listdir)
    readlink: Callable[[str], str] = field(default=os.readlink)
    # strict: a missing path raises instead of resolving to a made-up path,
    # which would make two missing components compare as different.
    realpath: Callable[[str], str] = field(
        default=lambda p: os.path.realpath(p, strict=True)
    )
    link_mtime: Callable[[str], float] = field(default=lambda p: os.lstat(p).st_mtime)
    # os.statvfs is Linux-only; resolved lazily so the module still imports
    # (and the tests still run) elsewhere.
    statvfs: Callable[[str], Any] = field(default=lambda p: os.statvfs(p))  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def parse_systemctl_show(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key] = value
    return out


def parse_meminfo(text: str) -> dict[str, int]:
    """/proc/meminfo, in kB, for the handful of fields worth reporting."""
    wanted = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in wanted:
            out[key] = int(rest.split()[0])
    return out


def parse_failed_units(text: str) -> list[str]:
    """First column of `systemctl list-units --state=failed --plain
    --no-legend`. Plain text rather than --output=json, which list-units
    only honours on newer systemd."""
    units = []
    for line in text.splitlines():
        parts = line.split()
        if parts:
            # A leading bullet appears on some versions even with --plain.
            units.append(
                parts[1] if parts[0] in ("●", "*") and len(parts) > 1 else parts[0]
            )
    return units


def tail_lines(text: str, n: int) -> list[str]:
    lines = text.splitlines()
    return lines[-n:] if n < len(lines) else lines


class Host:
    """The tools' implementation, independent of the MCP plumbing."""

    def __init__(
        self, cfg: Config, run: Runner = subprocess_runner, fs: Fs | None = None
    ):
        self.cfg = cfg
        self.run = run
        self.fs = fs or Fs()

    # -- helpers -----------------------------------------------------------

    def _sudo(self, helper: str) -> RunResult:
        # -n: never prompt. The rule is NOPASSWD; if it is missing we want an
        # immediate error, not a hang waiting for a password on stdin.
        return self.run([self.cfg.sudo, "-n", helper])

    def unit_state(self, unit: str) -> dict[str, Any]:
        res = self.run(
            [
                self.cfg.systemctl,
                "show",
                unit,
                "--no-pager",
                "--property=" + ",".join(UNIT_PROPERTIES),
            ]
        )
        if res.returncode != 0:
            return {"unit": unit, "error": _err(res)}
        return {"unit": unit, **parse_systemctl_show(res.stdout)}

    def _unit_busy(self, unit: str) -> bool | None:
        state = self.unit_state(unit)
        if "error" in state:
            return None
        return state.get("ActiveState") in BUSY_STATES

    def _journal(self, unit: str, lines: int) -> dict[str, Any]:
        helper = self.cfg.log_units.get(unit)
        if helper is None:
            return {"unit": unit, "error": "unit is not in the log allow-list"}
        res = self._sudo(helper)
        if res.returncode != 0:
            return {"unit": unit, "error": _err(res)}
        return {"unit": unit, "lines": tail_lines(res.stdout, lines)}

    # -- probes ------------------------------------------------------------

    def uptime(self) -> dict[str, Any]:
        seconds = float(self.fs.read_text("/proc/uptime").split()[0])
        load = self.fs.read_text("/proc/loadavg").split()
        return {
            "uptime_seconds": int(seconds),
            "load": {"1m": float(load[0]), "5m": float(load[1]), "15m": float(load[2])},
            "cpus": os.cpu_count(),
        }

    def memory(self) -> dict[str, Any]:
        info = parse_meminfo(self.fs.read_text("/proc/meminfo"))
        mib = {k: round(v / 1024) for k, v in info.items()}
        out: dict[str, Any] = {
            "total_mib": mib.get("MemTotal"),
            "available_mib": mib.get("MemAvailable"),
            "swap_total_mib": mib.get("SwapTotal"),
            "swap_free_mib": mib.get("SwapFree"),
        }
        if info.get("MemTotal"):
            used = 1 - info.get("MemAvailable", 0) / info["MemTotal"]
            out["used_percent"] = round(used * 100, 1)
        return out

    def disks(self) -> list[dict[str, Any]]:
        out = []
        for path in self.cfg.disk_paths:
            try:
                st = self.fs.statvfs(path)
            except OSError as exc:
                out.append({"path": path, "error": str(exc)})
                continue
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            out.append(
                {
                    "path": path,
                    "total_gib": round(total / 2**30, 1),
                    "free_gib": round(free / 2**30, 1),
                    "used_percent": round((1 - free / total) * 100, 1)
                    if total
                    else None,
                }
            )
        return out

    def failed_units(self) -> list[str] | dict[str, str]:
        res = self.run(
            [
                self.cfg.systemctl,
                "list-units",
                "--state=failed",
                "--plain",
                "--no-legend",
                "--no-pager",
            ]
        )
        if res.returncode != 0:
            return {"error": _err(res)}
        return parse_failed_units(res.stdout)

    def vm_state(self) -> dict[str, Any]:
        vm = self.cfg.vm
        if vm is None:
            return {"configured": False}
        res = self._sudo(vm.helper)
        if res.returncode != 0:
            return {"name": vm.name, "error": _err(res)}
        # `virsh domstate --reason` prints e.g. "running (booted)".
        text = res.stdout.strip()
        state, _, reason = text.partition(" (")
        return {"name": vm.name, "state": state, "reason": reason.rstrip(")") or None}

    def generations(self) -> dict[str, Any]:
        base = self.cfg.profiles_dir
        try:
            names = self.fs.listdir(base)
        except OSError as exc:
            return {"error": str(exc)}
        gens = []
        for name in names:
            m = GENERATION_LINK.match(name)
            if not m:
                continue
            path = f"{base}/{name}"
            try:
                created = _iso(self.fs.link_mtime(path))
            except OSError:
                created = None
            gens.append({"generation": int(m.group(1)), "created": created})
        gens.sort(key=lambda g: g["generation"])

        current = None
        try:
            m = GENERATION_LINK.match(self.fs.readlink(f"{base}/system"))
            current = int(m.group(1)) if m else None
        except OSError:
            pass
        previous = max(
            (
                g["generation"]
                for g in gens
                if current is None or g["generation"] < current
            ),
            default=None,
        )
        out: dict[str, Any] = {
            "current": current,
            "previous": previous,
            "recent": gens[-5:],
        }
        reasons = self.reboot_reasons()
        if reasons is not None:
            out["reboot_pending"] = bool(reasons)
            out["reboot_reasons"] = reasons
        return out

    def reboot_reasons(self) -> list[str] | None:
        """Which BOOT_COMPONENTS differ between the booted and the current
        system, or None if that can't be told (no /run/booted-system)."""

        def resolve(path: str) -> str | None:
            try:
                return self.fs.realpath(path)
            except FileNotFoundError:
                return None  # absent: equal to another absent one

        try:
            self.fs.realpath("/run/booted-system")
            self.fs.realpath("/run/current-system")
        except OSError:
            return None
        return [
            part
            for part in BOOT_COMPONENTS
            if resolve(f"/run/booted-system/{part}")
            != resolve(f"/run/current-system/{part}")
        ]

    # -- tools -------------------------------------------------------------

    def host_health(self) -> dict[str, Any]:
        sections: dict[str, Callable[[], Any]] = {
            "uptime": self.uptime,
            "memory": self.memory,
            "disks": self.disks,
            "failed_units": self.failed_units,
            "home_assistant_vm": self.vm_state,
            "generations": self.generations,
        }
        out: dict[str, Any] = {}
        for name, probe in sections.items():
            try:
                out[name] = probe()
            except Exception as exc:  # one broken probe must not hide the rest
                log.warning("probe %s failed: %s", name, exc)
                out[name] = {"error": f"{type(exc).__name__}: {exc}"}
        return out

    def glowbridge_status(self) -> dict[str, Any]:
        gb = self.cfg.glowbridge
        if gb is None:
            return {"configured": False}
        out: dict[str, Any] = {"service": self.unit_state(gb.unit)}
        res = self._sudo(gb.status_helper)
        if res.returncode != 0:
            out["status_file"] = {"error": _err(res)}
        else:
            try:
                out["status_file"] = json.loads(res.stdout)
            except json.JSONDecodeError as exc:
                out["status_file"] = {"error": f"status.json is not valid JSON: {exc}"}
        return out

    def service_logs(self, unit: str, lines: int) -> dict[str, Any]:
        if "." not in unit:
            unit += ".service"
        lines = max(1, min(int(lines), self.cfg.max_log_lines))
        out = self._journal(unit, lines)
        if "error" in out and unit not in self.cfg.log_units:
            out["allowed_units"] = sorted(self.cfg.log_units)
        return out

    def _start(self, job: JobConfig, other: JobConfig, what: str) -> dict[str, Any]:
        # The helper re-checks this as root; checking here as well just gives
        # a clearer answer than a bare helper exit code.
        for unit in (job.unit, other.unit):
            if self._unit_busy(unit):
                return {"started": False, "reason": f"{unit} is already running"}
        previous = self.last_run(job)
        res = self._sudo(job.start_helper)
        if res.returncode != 0:
            return {"started": False, "reason": _err(res)}
        return {
            "started": True,
            "unit": job.unit,
            # Compare with upgrade_status's last_run to tell this run's
            # result from the one before it.
            "previous_run": previous,
            "next": f"{what} runs in the background and takes minutes; poll"
            " upgrade_status until the unit is no longer activating.",
        }

    def last_run(self, job: JobConfig) -> dict[str, Any] | None:
        """The job's last completed run, or None if it has never finished
        one (or the module predates the record)."""
        if job.last_run_file is None:
            return None
        try:
            record = json.loads(self.fs.read_text(job.last_run_file))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            return {"error": f"cannot read {job.last_run_file}: {exc}"}
        return record if isinstance(record, dict) else {"error": "malformed record"}

    def system_upgrade(self) -> dict[str, Any]:
        out = self._start(self.cfg.upgrade, self.cfg.rollback, "The upgrade")
        if self.cfg.upgrade_source:
            out["source"] = self.cfg.upgrade_source
        return out

    def system_rollback(self) -> dict[str, Any]:
        return self._start(self.cfg.rollback, self.cfg.upgrade, "The rollback")

    def upgrade_status(self, log_lines: int = 60) -> dict[str, Any]:
        log_lines = max(1, min(int(log_lines), self.cfg.max_log_lines))
        out: dict[str, Any] = {}
        for key, job in (
            ("upgrade", self.cfg.upgrade),
            ("rollback", self.cfg.rollback),
        ):
            state = self.unit_state(job.unit)
            # Only the live state comes from systemd: once the idle unit is
            # unloaded, its Result and timestamps read back as defaults
            # ("success", blank), which would misreport a finished run.
            entry: dict[str, Any] = {"unit": job.unit}
            for prop in ("error", "ActiveState", "SubState"):
                if prop in state:
                    entry[prop] = state[prop]
            entry["last_run"] = self.last_run(job)
            journal = self._journal(job.unit, log_lines)
            entry["log"] = journal.get("lines", journal.get("error"))
            out[key] = entry
        out["generations"] = self.generations()
        if self.cfg.upgrade_source:
            out["source"] = self.cfg.upgrade_source
        return out


def _err(res: RunResult) -> str:
    detail = (res.stderr or res.stdout).strip().splitlines()
    return f"exit {res.returncode}" + (f": {detail[-1]}" if detail else "")


# --------------------------------------------------------------------------
# MCP wiring
# --------------------------------------------------------------------------


def build_server(host: Host):
    # Imported here so the probes above can be imported (and tested) without
    # the MCP SDK's import cost.
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        "ha-server",
        instructions=(
            "Read-only health and log access to a NixOS home server, plus"
            " fixed upgrade and rollback jobs. There is no way to run an"
            " arbitrary command; config changes reach the host through its"
            " git repository."
        ),
    )

    @mcp.tool()
    def host_health() -> dict[str, Any]:
        """Uptime, load, memory, disk usage, failed systemd units, the Home
        Assistant VM's state and the current NixOS generation. Read-only."""
        return host.host_health()

    @mcp.tool()
    def glowbridge_status() -> dict[str, Any]:
        """glowbridge's systemd state and its status.json (last success,
        consecutive failures, per-meter frontier). Read-only."""
        return host.glowbridge_status()

    @mcp.tool()
    def service_logs(unit: str, lines: int = 100) -> dict[str, Any]:
        """The last `lines` journal lines for one allow-listed systemd unit
        (call with an unknown unit to see the allow-list). Read-only."""
        return host.service_logs(unit, lines)

    @mcp.tool()
    def system_upgrade() -> dict[str, Any]:
        """Start the host's fixed NixOS upgrade job, which rebuilds and
        switches to the configured git branch. Returns immediately; poll
        upgrade_status for the result."""
        return host.system_upgrade()

    @mcp.tool()
    def upgrade_status(log_lines: int = 60) -> dict[str, Any]:
        """Live state, last completed run (start, finish, result, exit
        status) and log tail of the upgrade and rollback jobs, plus the
        current and previous NixOS generations. Read-only."""
        return host.upgrade_status(log_lines)

    @mcp.tool()
    def system_rollback() -> dict[str, Any]:
        """Start the fixed rollback job: switch to the generation before the
        current one. Running it twice goes back two generations. Returns
        immediately; poll upgrade_status."""
        return host.system_rollback()

    return mcp


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ha-server-mcp",
        description="Narrowly scoped MCP server (stdio) for a NixOS home server.",
    )
    p.add_argument(
        "--config", type=Path, help="JSON config rendered by the NixOS module"
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    # stderr only: stdout carries the protocol.
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="ha-server-mcp: %(message)s"
    )
    if args.config is None:
        print("ha-server-mcp: --config is required", file=sys.stderr)
        return 2
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"ha-server-mcp: configuration error: {exc}", file=sys.stderr)
        return 2
    build_server(Host(cfg)).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
