# SPDX-License-Identifier: MIT
"""MCP client driven by nix/vm-test.nix. Connects exactly the way the real
client does — ssh as the restricted user, whose forced command is the
server — and checks each tool end to end. Exits non-zero on any failure.

Usage: python vm-test-client.py <private-key> <fake-secret>
"""

import asyncio
import json
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

KEY, SECRET = sys.argv[1], sys.argv[2]

EXPECTED_TOOLS = {
    "host_health",
    "glowbridge_status",
    "service_logs",
    "system_upgrade",
    "upgrade_status",
    "system_rollback",
}


def payload(result):
    assert not result.isError, result
    if result.structuredContent is not None:
        data = result.structuredContent
        return data.get("result", data)
    return json.loads(result.content[0].text)


async def wait_for(session, job, timeout=300):
    """Poll upgrade_status until `job` has finished, including its
    ExecStopPost record (written while the unit is deactivating)."""
    deadline = time.monotonic() + timeout
    while True:
        st = payload(await session.call_tool("upgrade_status", {"log_lines": 20}))
        if st[job].get("ActiveState") not in ("activating", "active", "deactivating"):
            return st
        assert time.monotonic() < deadline, f"{job} never finished"
        await asyncio.sleep(2)


async def main():
    params = StdioServerParameters(
        command="ssh",
        args=[
            "-i", KEY,
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            # Asking for a command must make no difference: sshd runs the
            # forced command regardless.
            "claude@127.0.0.1", "id",
        ],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            tools = {t.name for t in (await s.list_tools()).tools}
            assert tools == EXPECTED_TOOLS, tools

            health = payload(await s.call_tool("host_health", {}))
            print("host_health:", json.dumps(health))
            assert health["uptime"]["uptime_seconds"] >= 0
            assert isinstance(health["failed_units"], list), health["failed_units"]
            assert health["home_assistant_vm"] == {"configured": False}

            gb = payload(await s.call_tool("glowbridge_status", {}))
            print("glowbridge_status:", json.dumps(gb))
            assert gb["service"]["ActiveState"] == "active", gb
            assert gb["status_file"] == {"schema": 1, "ok": True}, gb
            assert SECRET not in json.dumps(gb)

            logs = payload(await s.call_tool("service_logs", {"unit": "glowbridge"}))
            assert any("fake glowbridge up" in ln for ln in logs["lines"]), logs
            denied = payload(await s.call_tool("service_logs", {"unit": "sshd"}))
            assert "allow-list" in denied["error"], denied

            started = payload(await s.call_tool("system_upgrade", {}))
            print("system_upgrade:", json.dumps(started))
            assert started["started"] is True, started
            assert started["previous_run"] is None, started  # first run

            # The VM has no network, so the upgrade fails fast — which still
            # proves the unit, its environment and its log path are wired.
            st = await wait_for(s, "upgrade")
            print("upgrade_status:", json.dumps(st))
            last = st["upgrade"]["last_run"]
            # The unit's own record, not systemd's (which forgets a finished
            # oneshot): a failed fetch must read as a failure.
            assert last["result"] == "exit-code", last
            assert last["exit_status"] == "1", last
            assert last["started_at"] and last["finished_at"], last
            assert st["upgrade"]["log"], st

            rb = payload(await s.call_tool("system_rollback", {}))
            print("system_rollback:", json.dumps(rb))
            assert rb["started"] is True, rb
            st = await wait_for(s, "rollback")
            print("upgrade_status after rollback:", json.dumps(st))
            # Whatever the rollback did in a VM with no profile history, it
            # finished and recorded an outcome.
            assert st["rollback"]["last_run"]["result"], st
    print("vm-test-client: all checks passed")


asyncio.run(main())
