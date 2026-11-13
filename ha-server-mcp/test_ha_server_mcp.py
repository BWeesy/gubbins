#!/usr/bin/env -S uv run --script
# SPDX-License-Identifier: MIT
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest>=8",
#     "mcp>=1.29,<2",
# ]
# ///
"""Tests for ha-server-mcp.

The server's whole job is to be narrow, so the tests concentrate on that:
the exact tool surface (and that no tool takes a command or path), that
privileged work only ever goes through `sudo -n <configured helper>` with no
caller-supplied arguments, the log allow-list and line cap, refusing to start
a job while one is running, and per-section degradation of host_health. The
host is driven through a fake runner and fake filesystem; there are
deliberately no tests against a live machine (nix/vm-test.nix covers the
real wiring).

Run: uv run test_ha_server_mcp.py   (or: pytest test_ha_server_mcp.py)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ha_server_mcp as hsm

SUDO = "/run/wrappers/bin/sudo"
SYSTEMCTL = "/sys/bin/systemctl"

RAW_CONFIG = {
    "sudo": SUDO,
    "systemctl": SYSTEMCTL,
    "max_log_lines": 50,
    "disk_paths": ["/", "/nix"],
    "profiles_dir": "/nix/var/nix/profiles",
    "upgrade_source": "git+ssh://git@example.com/host?ref=main#host",
    "glowbridge": {"unit": "glowbridge.service", "status_helper": "/h/gb-status"},
    "vm": {"name": "haos", "helper": "/h/vm-state"},
    "log_units": {
        "glowbridge.service": "/h/journal-glowbridge",
        "upgrade.service": "/h/journal-upgrade",
        "rollback.service": "/h/journal-rollback",
    },
    "upgrade": {"unit": "upgrade.service", "start_helper": "/h/start-upgrade"},
    "rollback": {"unit": "rollback.service", "start_helper": "/h/start-rollback"},
}

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeRunner:
    """Records every argv and answers from a table. Anything unanswered is a
    127 'not found', which is also how a missing binary looks for real."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.sudo: dict[str, hsm.RunResult] = {}
        self.units: dict[str, dict[str, str]] = {}
        self.failed = ""

    def ok(self, helper: str, stdout: str) -> None:
        self.sudo[helper] = hsm.RunResult(0, stdout, "")

    def __call__(self, argv: list[str]) -> hsm.RunResult:
        self.calls.append(list(argv))
        if argv[0] == SUDO:
            assert argv[1] == "-n" and len(argv) == 3, argv
            return self.sudo.get(
                argv[2], hsm.RunResult(1, "", "sudo: a password is required")
            )
        if argv[:2] == [SYSTEMCTL, "show"]:
            props = self.units.get(
                argv[2], {"ActiveState": "inactive", "Result": "success"}
            )
            return hsm.RunResult(0, "".join(f"{k}={v}\n" for k, v in props.items()), "")
        if argv[:2] == [SYSTEMCTL, "list-units"]:
            return hsm.RunResult(0, self.failed, "")
        return hsm.RunResult(127, "", f"{argv[0]}: not found")

    def sudo_helpers(self) -> list[str]:
        return [c[2] for c in self.calls if c[0] == SUDO]


def fake_fs(**overrides) -> hsm.Fs:
    files = {
        "/proc/uptime": "93784.12 100.0\n",
        "/proc/loadavg": "0.50 0.40 0.30 1/200 999\n",
        "/proc/meminfo": "MemTotal: 16384000 kB\nMemFree: 1 kB\n"
        "MemAvailable: 4096000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
    }
    links = {
        "/nix/var/nix/profiles/system": "system-42-link",
    }
    real = {"/run/current-system": "/nix/store/b", "/run/booted-system": "/nix/store/a"}

    def read_text(p):
        try:
            return files[p]
        except KeyError:
            raise FileNotFoundError(p) from None

    fs = hsm.Fs(
        read_text=read_text,
        listdir=lambda p: [
            "system",
            "system-40-link",
            "system-41-link",
            "system-42-link",
            "other",
        ],
        readlink=lambda p: links[p],
        realpath=lambda p: real[p],
        link_mtime=lambda p: 1_700_000_000.0,
        statvfs=lambda p: SimpleNamespace(f_blocks=1000, f_bavail=250, f_frsize=2**20),
    )
    for k, v in overrides.items():
        setattr(fs, k, v)
    return fs


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def host(runner):
    return hsm.Host(hsm.parse_config(RAW_CONFIG), run=runner, fs=fake_fs())


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_config_round_trip():
    cfg = hsm.parse_config(RAW_CONFIG)
    assert cfg.vm == hsm.VmConfig("haos", "/h/vm-state")
    assert cfg.disk_paths == ("/", "/nix")
    assert cfg.upgrade.start_helper == "/h/start-upgrade"


def test_config_optional_sections_may_be_null():
    raw = {**RAW_CONFIG, "vm": None, "glowbridge": None}
    cfg = hsm.parse_config(raw)
    assert cfg.vm is None and cfg.glowbridge is None


@pytest.mark.parametrize(
    "patch",
    [
        {"sudo": None},
        {"upgrade": {"unit": "x"}},
        {"log_units": {"a.service": 3}},
        {"max_log_lines": 0},
        {"max_log_lines": True},
        {"disk_paths": "/"},
    ],
)
def test_config_rejects_bad_shapes(patch):
    with pytest.raises(hsm.ConfigError):
        hsm.parse_config({**RAW_CONFIG, **patch})


def test_load_config_reports_unreadable(tmp_path):
    bad = tmp_path / "c.json"
    bad.write_text("{nope", encoding="utf-8")
    with pytest.raises(hsm.ConfigError):
        hsm.load_config(bad)


def test_main_requires_config(capsys):
    assert hsm.main([]) == 2
    assert "--config" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Tool surface
# --------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "host_health",
    "glowbridge_status",
    "service_logs",
    "system_upgrade",
    "upgrade_status",
    "system_rollback",
}


def _tools(host):
    return asyncio.run(hsm.build_server(host).list_tools())


def test_tool_surface_is_exactly_the_six(host):
    assert {t.name for t in _tools(host)} == EXPECTED_TOOLS


def test_no_tool_accepts_a_command_or_path(host):
    # The only caller inputs anywhere are a unit name (checked against the
    # allow-list) and line counts. Anything else is a design regression.
    allowed = {"service_logs": {"unit", "lines"}, "upgrade_status": {"log_lines"}}
    for tool in _tools(host):
        params = set(tool.inputSchema.get("properties", {}))
        assert params == allowed.get(tool.name, set()), tool.name


def test_tools_call_through_mcp(host, runner):
    runner.ok("/h/journal-glowbridge", "a\nb\nc\n")
    server = hsm.build_server(host)
    result = asyncio.run(
        server.call_tool("service_logs", {"unit": "glowbridge", "lines": 2})
    )
    # FastMCP returns (content, structured) for dict-returning tools.
    structured = result[1] if isinstance(result, tuple) else result
    payload = structured.get("result", structured)
    assert payload["lines"] == ["b", "c"]


# --------------------------------------------------------------------------
# host_health
# --------------------------------------------------------------------------


def test_host_health_happy_path(host, runner):
    runner.ok("/h/vm-state", "running (booted)\n")
    runner.failed = (
        "foo.service loaded failed failed Foo\n● bar.timer loaded failed failed Bar\n"
    )
    h = host.host_health()
    assert h["uptime"]["uptime_seconds"] == 93784
    assert h["uptime"]["load"] == {"1m": 0.5, "5m": 0.4, "15m": 0.3}
    assert h["memory"]["total_mib"] == 16000
    assert h["memory"]["used_percent"] == 75.0
    assert h["disks"][0] == {
        "path": "/",
        "total_gib": 1.0,
        "free_gib": 0.2,
        "used_percent": 75.0,
    }
    assert h["failed_units"] == ["foo.service", "bar.timer"]
    assert h["home_assistant_vm"] == {
        "name": "haos",
        "state": "running",
        "reason": "booted",
    }
    gens = h["generations"]
    assert (gens["current"], gens["previous"]) == (42, 41)
    assert [g["generation"] for g in gens["recent"]] == [40, 41, 42]
    assert gens["reboot_pending"] is True


def test_host_health_degrades_per_section(runner):
    def boom(p):
        raise PermissionError("denied")

    host = hsm.Host(
        hsm.parse_config(RAW_CONFIG), run=runner, fs=fake_fs(read_text=boom)
    )
    # No vm helper answer -> sudo refuses; /proc unreadable.
    h = host.host_health()
    assert "error" in h["uptime"] and "error" in h["memory"]
    assert "error" in h["home_assistant_vm"]
    # ...but the sections that could run still did.
    assert h["disks"][0]["path"] == "/"
    assert h["generations"]["current"] == 42


def test_vm_not_configured(runner):
    host = hsm.Host(
        hsm.parse_config({**RAW_CONFIG, "vm": None}), run=runner, fs=fake_fs()
    )
    assert host.vm_state() == {"configured": False}
    assert runner.sudo_helpers() == []


def test_vm_missing_virsh_reports_error(host, runner):
    runner.sudo["/h/vm-state"] = hsm.RunResult(127, "", "virsh: command not found")
    assert host.vm_state() == {
        "name": "haos",
        "error": "exit 127: virsh: command not found",
    }


def test_disk_error_is_per_path(runner):
    def statvfs(p):
        if p == "/nix":
            raise FileNotFoundError("no such mount")
        return SimpleNamespace(f_blocks=10, f_bavail=5, f_frsize=1)

    host = hsm.Host(
        hsm.parse_config(RAW_CONFIG), run=runner, fs=fake_fs(statvfs=statvfs)
    )
    disks = host.disks()
    assert disks[0]["used_percent"] == 50.0
    assert disks[1] == {"path": "/nix", "error": "no such mount"}


def test_parse_failed_units_empty():
    assert hsm.parse_failed_units("") == []


# --------------------------------------------------------------------------
# glowbridge_status
# --------------------------------------------------------------------------


def test_glowbridge_status_reads_only_the_status_helper(host, runner):
    runner.units["glowbridge.service"] = {
        "ActiveState": "active",
        "SubState": "running",
    }
    runner.ok(
        "/h/gb-status",
        json.dumps({"consecutive_failures": 0, "bridge_version": "1.0.1"}),
    )
    out = host.glowbridge_status()
    assert out["service"]["ActiveState"] == "active"
    assert out["status_file"]["bridge_version"] == "1.0.1"
    assert runner.sudo_helpers() == ["/h/gb-status"]
    # The server itself never names state.json anywhere.
    assert not any("state.json" in " ".join(c) for c in runner.calls)


def test_glowbridge_status_bad_json(host, runner):
    runner.ok("/h/gb-status", "{truncated")
    assert "not valid JSON" in host.glowbridge_status()["status_file"]["error"]


def test_glowbridge_status_sudo_refused(host):
    assert "password is required" in host.glowbridge_status()["status_file"]["error"]


# --------------------------------------------------------------------------
# service_logs
# --------------------------------------------------------------------------


def test_logs_allow_listed_unit_with_cap(host, runner):
    runner.ok("/h/journal-glowbridge", "".join(f"line {i}\n" for i in range(200)))
    out = host.service_logs("glowbridge.service", 10_000)
    assert len(out["lines"]) == 50  # max_log_lines
    assert out["lines"][-1] == "line 199"


def test_logs_bare_name_gets_service_suffix(host, runner):
    runner.ok("/h/journal-glowbridge", "x\n")
    assert host.service_logs("glowbridge", 5)["lines"] == ["x"]


def test_logs_refuse_unlisted_unit_without_running_anything(host, runner):
    out = host.service_logs("sshd.service", 5)
    assert "allow-list" in out["error"]
    assert "glowbridge.service" in out["allowed_units"]
    assert runner.calls == []


@pytest.mark.parametrize("unit", ["../etc/shadow", "glowbridge.service --all", "-f"])
def test_logs_hostile_unit_names_never_reach_a_process(host, runner, unit):
    assert "error" in host.service_logs(unit, 5)
    assert runner.calls == []


def test_logs_lines_floor(host, runner):
    runner.ok("/h/journal-glowbridge", "a\nb\n")
    assert host.service_logs("glowbridge", -3)["lines"] == ["b"]


# --------------------------------------------------------------------------
# upgrade / rollback
# --------------------------------------------------------------------------


def test_upgrade_starts_fixed_helper(host, runner):
    runner.ok("/h/start-upgrade", "")
    runner.units["upgrade.service"] = {
        "ActiveState": "inactive",
        "ExecMainStartTimestamp": "Thu 2026-11-12 10:00:00 GMT",
    }
    out = host.system_upgrade()
    assert out["started"] is True
    assert out["previous_run_started"] == "Thu 2026-11-12 10:00:00 GMT"
    assert out["source"].startswith("git+ssh://")
    assert runner.sudo_helpers() == ["/h/start-upgrade"]


@pytest.mark.parametrize("busy_unit", ["upgrade.service", "rollback.service"])
def test_upgrade_refused_while_a_job_runs(host, runner, busy_unit):
    runner.ok("/h/start-upgrade", "")
    runner.units[busy_unit] = {"ActiveState": "activating"}
    out = host.system_upgrade()
    assert out == {
        "started": False,
        "reason": f"{busy_unit} is already running",
        "source": RAW_CONFIG["upgrade_source"],
    }
    assert runner.sudo_helpers() == []


def test_upgrade_helper_failure_is_reported(host, runner):
    runner.sudo["/h/start-upgrade"] = hsm.RunResult(3, "", "rollback in progress")
    out = host.system_upgrade()
    assert out["started"] is False and "rollback in progress" in out["reason"]


def test_rollback_starts_fixed_helper(host, runner):
    runner.ok("/h/start-rollback", "")
    assert host.system_rollback()["started"] is True
    assert runner.sudo_helpers() == ["/h/start-rollback"]


def test_upgrade_status(host, runner):
    runner.units["upgrade.service"] = {
        "ActiveState": "inactive",
        "Result": "exit-code",
        "ExecMainStatus": "1",
    }
    runner.ok("/h/journal-upgrade", "building...\nerror: boom\n")
    out = host.upgrade_status(log_lines=1)
    assert out["upgrade"]["Result"] == "exit-code"
    assert out["upgrade"]["log"] == ["error: boom"]
    assert out["rollback"]["log"].startswith("exit 1")  # helper not answered
    assert out["generations"]["current"] == 42


def test_subprocess_runner_missing_binary():
    res = hsm.subprocess_runner(["/definitely/not/here"])
    assert res.returncode == 127


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
