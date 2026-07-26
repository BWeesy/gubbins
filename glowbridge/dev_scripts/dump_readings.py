#!/usr/bin/env -S uv run --script
# SPDX-License-Identifier: MIT
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests>=2.31",
# ]
# ///
"""One-off: dump raw Glowmarkt readings forward from a timestamp to a file.

A development / exploration tool, deliberately separate from the bridge. It
walks the API forward from a given start time and records every reading row
exactly as returned — nulls included, no rounding, no watermark, no MQTT —
into a single JSON file, so the real payload shape can be examined by hand.

It reuses glowbridge's config loader, auth client and redacting logger (so
credentials never reach the log) but issues the /readings GET itself, on
purpose: glowbridge.get_readings drops nulls and converts to internal
units, which is exactly the transformation you do NOT want when the point
is to see what the DCC actually sent.

Usage:
    uv run --script dump_readings.py --from 2026-07-19
    uv run --script dump_readings.py --from 2026-07-19T00:00 --to 2026-07-21
    uv run --script dump_readings.py --from 2026-07-19 --resource <id> --all

The output file (default glowbridge/dev_scripts/glow_dump.json) is gitignored.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

# Import the bridge as a library, the same way the test suite does, so this
# script tracks the one true API contract rather than duplicating it. The
# parent directory is the glowbridge package folder (this lives in
# glowbridge/dev_scripts/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import glowbridge as gb

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "glow_dump.json"


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO date or datetime, assuming UTC if no zone is given."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(
            f"dump_readings: cannot parse timestamp {value!r}"
            " (use e.g. 2026-07-19 or 2026-07-19T00:00)"
        ) from exc
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _get_readings_once(
    client: gb.GlowmarktClient, resource_id: str, start: datetime, end: datetime
) -> requests.Response:
    params = {
        "from": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "to": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "period": "PT30M",
        "function": "sum",
        "offset": 0,
        "nulls": 1,
    }
    return client.session.get(
        f"{gb.GLOWMARKT_BASE_URL}/resource/{resource_id}/readings",
        params=params,
        timeout=(client.CONNECT_TIMEOUT, client.READ_TIMEOUT),
    )


def fetch_raw_readings(
    client: gb.GlowmarktClient,
    resource_id: str,
    start: datetime,
    end: datetime,
    max_retries: int,
) -> tuple[list, dict]:
    """GET one /readings window verbatim, with retries.

    Returns the raw ``data`` rows and the response envelope with ``data``
    stripped out (so units/classifier/other fields can be inspected without
    the bulk of the rows). A 6-month pull is ~50 sequential requests against
    a flaky API, so transient failures are retried with exponential backoff;
    a 429 honours Retry-After; a stale token re-auths once. Timeouts and
    headers come from the shared client session, so this is the bridge's
    request minus the null-dropping transform.
    """
    reauthed = False
    for attempt in range(1, max_retries + 1):
        try:
            resp = _get_readings_once(client, resource_id, start, end)
            if resp.status_code == 401 and not reauthed:
                gb.log.info("token rejected; re-authenticating")
                client.authenticate()
                reauthed = True
                continue
            if resp.status_code == 429:
                wait = gb._retry_after_seconds(resp) or _backoff(attempt)
                gb.log.warning("rate limited; waiting %ss", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            body = resp.json()
            rows = body.get("data")
            if not isinstance(rows, list):
                rows = []
            envelope = {k: v for k, v in body.items() if k != "data"}
            return rows, envelope
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise
            wait = _backoff(attempt)
            gb.log.warning(
                "chunk %s..%s failed (attempt %d/%d): %s; retrying in %ss",
                start.isoformat(), end.isoformat(), attempt, max_retries, exc, wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"exhausted {max_retries} attempts for {resource_id}")


def _backoff(attempt: int) -> int:
    return min(60, 2 ** attempt)


def resolve_resources(
    client: gb.GlowmarktClient, wanted: list[str], include_all: bool
) -> list[dict]:
    """Decide which resources to dump.

    With no --resource pins: every discovered resource if --all, otherwise
    just the consumption classifiers the bridge cares about. With pins: those
    exact IDs, annotated from discovery where possible so the output still
    carries a name and classifier.
    """
    discovered = {
        r.get("resourceId", ""): r for r in client.get_resources() if r.get("resourceId")
    }
    if wanted:
        out = []
        for rid in wanted:
            meta = discovered.get(rid, {})
            out.append(
                {
                    "resourceId": rid,
                    "name": meta.get("name", "(pinned, not in discovery)"),
                    "classifier": meta.get("classifier", "unknown"),
                }
            )
        return out
    selected = discovered.values()
    if not include_all:
        selected = [
            r
            for r in selected
            if r.get("classifier") in gb.BRIDGED_CLASSIFIERS
        ]
    return list(selected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dump_readings",
        description="Dump raw Glowmarkt readings forward from a timestamp.",
    )
    parser.add_argument("--config", type=Path, default=None,
                        help="glowbridge config file (default: the bridge's own path)")
    parser.add_argument("--from", dest="start", required=True,
                        help="start timestamp, ISO (e.g. 2026-07-19 or 2026-07-19T00:00)")
    parser.add_argument("--to", dest="end", default=None,
                        help="end timestamp, ISO (default: now)")
    parser.add_argument("--resource", action="append", default=[],
                        help="resource ID to dump; repeatable (default: discovered consumption)")
    parser.add_argument("--all", action="store_true",
                        help="with no --resource, dump every resource, not just consumption")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"output JSON file (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--max-retries", type=int, default=6,
                        help="retries per chunk before giving up (default: 6)")
    parser.add_argument("--pause", type=float, default=1.0,
                        help="seconds to wait between requests, to be gentle (default: 1.0)")
    parser.add_argument("--debug", action="store_true", help="debug logging (redacted)")
    args = parser.parse_args(argv)

    config_path = args.config or gb.default_config_path()
    try:
        cfg = gb.load_config(config_path)
    except gb.ConfigError as exc:
        print(f"dump_readings: configuration error: {exc}", file=sys.stderr)
        return 2
    gb.setup_logging(cfg, args.debug)

    start = gb.floor_half_hour(parse_timestamp(args.start))
    end = parse_timestamp(args.end) if args.end else datetime.now(tz=UTC)
    if end <= start:
        print("dump_readings: --to must be after --from", file=sys.stderr)
        return 2

    client = gb.GlowmarktClient(cfg.glowmarkt)
    client.authenticate()  # fresh token; this script never touches the bridge state file
    gb.log.info("authenticated; dumping %s -> %s", start.isoformat(), end.isoformat())

    resources = resolve_resources(client, args.resource, args.all)
    if not resources:
        print("dump_readings: no resources selected", file=sys.stderr)
        return 1

    out: dict = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "base_url": gb.GLOWMARKT_BASE_URL,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "resources": {},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def flush() -> None:
        # Write after every chunk so a mid-run failure keeps its progress.
        # Rows are keyed by epoch, so just re-running the same command folds
        # any re-fetched windows back in idempotently — that is the "resume".
        args.output.write_text(json.dumps(out, indent=2), encoding="utf-8")

    for res in resources:
        rid = res["resourceId"]
        # Keyed by epoch so any overlap at chunk boundaries collapses to one
        # row per interval rather than double-recording it.
        rows_by_ts: dict[int, list] = {}
        envelope: dict = {}
        entry = {
            "name": res.get("name", ""),
            "classifier": res.get("classifier", ""),
            "response_meta": envelope,
            "reading_count": 0,
            "null_count": 0,
            "readings": [],
        }
        out["resources"][rid] = entry
        for window_start, window_end in gb.fetch_windows(start, end):
            rows, env = fetch_raw_readings(
                client, rid, window_start, window_end, args.max_retries
            )
            envelope = envelope or env
            for row in rows:
                if isinstance(row, list) and row:
                    rows_by_ts[row[0]] = row
            readings = [rows_by_ts[ts] for ts in sorted(rows_by_ts)]
            entry["response_meta"] = envelope
            entry["readings"] = readings
            entry["reading_count"] = len(readings)
            entry["null_count"] = sum(
                1 for r in readings if len(r) < 2 or r[1] is None
            )
            gb.log.info(
                "%s %s..%s: %d row(s) (%d total)",
                rid, window_start.isoformat(), window_end.isoformat(),
                len(rows), len(readings),
            )
            flush()
            if args.pause:
                time.sleep(args.pause)
        gb.log.info(
            "%s (%s): %d rows, %d null",
            rid, res.get("classifier", "?"),
            entry["reading_count"], entry["null_count"],
        )

    flush()
    total = sum(r["reading_count"] for r in out["resources"].values())
    print(
        f"dump_readings: wrote {total} reading(s) across"
        f" {len(out['resources'])} resource(s) to {args.output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.RequestException as exc:
        print(f"dump_readings: request failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
