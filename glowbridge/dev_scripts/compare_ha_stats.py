#!/usr/bin/env -S uv run --script
# SPDX-License-Identifier: MIT
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests>=2.31",
#     "websocket-client>=1.7",
# ]
# ///
"""Compare Home Assistant's imported statistics against the Glow API.

A one-off validation tool. For each bridged meter it reads the hourly
consumption Home Assistant has stored (via the recorder/statistics_during_period
WebSocket command) and the hourly energy the Glow API reports for the same
window, aligns them by hour, and prints the per-hour differences and totals.
Read-only against both HA and Glow; writes nothing.

It reuses glowbridge's config, auth client and helpers, so it talks to the
same HA and Glow endpoints the daemon does. Run it after glowbridge has
imported some data.

Usage:
    uv run --script compare_ha_stats.py --from 2026-07-20 --to 2026-07-27
    uv run --script compare_ha_stats.py --from 2026-07-20 --tolerance 0.001
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import websocket

# Import the bridge as a library (the glowbridge package dir is the parent).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import glowbridge as gb


def parse_ts(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"cannot parse timestamp {value!r}") from exc
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _to_dt(value) -> datetime:
    # HA returns statistic `start` as a unix-ms number on recent versions,
    # an ISO string on older ones.
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    return datetime.fromisoformat(value).astimezone(UTC)


def read_ha_statistics(
    cfg: gb.Config, statistic_ids: list[str], start: datetime, end: datetime
) -> dict[str, list[dict]]:
    """Read hourly statistics from HA. Requests the per-hour `change` (and
    `sum` as a fallback), which is exactly the consumption we compare."""
    ws = websocket.create_connection(cfg.homeassistant.url, timeout=30)
    try:
        ws.recv()  # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": cfg.homeassistant.token}))
        if json.loads(ws.recv()).get("type") != "auth_ok":
            raise SystemExit("Home Assistant rejected the access token")
        ws.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "recorder/statistics_during_period",
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "statistic_ids": statistic_ids,
                    "period": "hour",
                    "types": ["change", "sum"],
                }
            )
        )
        result = json.loads(ws.recv())
    finally:
        ws.close()
    if not result.get("success"):
        raise SystemExit(f"HA statistics query failed: {result.get('error')}")
    return result.get("result", {})


def ha_hourly_consumption(rows: list[dict]) -> dict[datetime, float]:
    """Per-hour consumption keyed by hour start. Prefer HA's `change`; fall
    back to differencing consecutive `sum` values when change is absent."""
    by_start = {_to_dt(r["start"]): r for r in rows}
    out: dict[datetime, float] = {}
    for start, row in by_start.items():
        if row.get("change") is not None:
            out[start] = float(row["change"])
        elif row.get("sum") is not None:
            prev = by_start.get(start - timedelta(hours=1))
            if prev is not None and prev.get("sum") is not None:
                out[start] = float(row["sum"]) - float(prev["sum"])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compare_ha_stats",
        description="Diff Home Assistant's imported statistics against the Glow API.",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--from", dest="start", required=True, help="ISO start")
    parser.add_argument("--to", dest="end", default=None, help="ISO end (default: now)")
    parser.add_argument(
        "--tolerance", type=float, default=0.0005,
        help="per-hour kWh difference to treat as a match (default 0.0005)",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    cfg = gb.load_config(args.config or gb.default_config_path())
    gb.setup_logging(cfg, args.debug)
    if not cfg.homeassistant.token:
        raise SystemExit(f"Home Assistant token not set ({gb.ENV_HA_TOKEN})")

    start = gb.floor_hour(parse_ts(args.start))
    end = gb.floor_hour(parse_ts(args.end)) if args.end else datetime.now(tz=UTC)

    client = gb.GlowmarktClient(cfg.glowmarkt)
    client.authenticate()
    sid_map: dict[str, str] = {}  # statistic_id -> resource_id
    for res in client.get_resources():
        classifier = res.get("classifier", "")
        rid = res.get("resourceId", "")
        if rid and classifier in gb.BRIDGED_CLASSIFIERS:
            sid_map[gb.statistic_id_for(rid, classifier)] = rid

    # Query HA from one hour before `start` so the first in-range hour has a
    # predecessor when falling back to sum-differencing.
    ha = read_ha_statistics(cfg, list(sid_map), start - timedelta(hours=1), end)

    exit_code = 0
    for sid, rid in sid_map.items():
        glow: dict[datetime, float] = {}
        for a, b in gb.fetch_windows(start, end):
            for r in client.get_readings(rid, a, b):
                glow[r.start] = r.kwh
        ha_hours = ha_hourly_consumption(ha.get(sid, []))

        hours = sorted(h for h in set(glow) | set(ha_hours) if start <= h < end)
        print(f"\n=== {sid}  ({rid}) ===")
        glow_total = ha_total = 0.0
        mismatches = 0
        max_diff = 0.0
        for h in hours:
            g = glow.get(h)
            a = ha_hours.get(h)
            glow_total += g or 0.0
            ha_total += a or 0.0
            if g is None or a is None:
                mismatches += 1
                print(f"  {h.isoformat()}  glow={g!s:>8}  ha={a!s:>8}  MISSING")
                continue
            diff = abs(g - a)
            max_diff = max(max_diff, diff)
            if diff > args.tolerance:
                mismatches += 1
                print(f"  {h.isoformat()}  glow={g:8.3f}  ha={a:8.3f}  diff={diff:.3f}")
        print(
            f"  hours={len(hours)}  glow_total={glow_total:.3f}kWh"
            f"  ha_total={ha_total:.3f}kWh  max_hourly_diff={max_diff:.3f}"
            f"  mismatches={mismatches}"
        )
        if mismatches:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, websocket.WebSocketException) as exc:
        print(f"compare_ha_stats: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
