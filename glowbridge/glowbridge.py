#!/usr/bin/env -S uv run --script
# SPDX-License-Identifier: MIT
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests>=2.31",
#     "websocket-client>=1.7",
# ]
# ///
"""glowbridge: import UK smart meter readings from the Glowmarkt/DCC API into
Home Assistant as long-term statistics.

Fetches finalised hourly consumption from the Glowmarkt API (the backend
behind Hildebrand's Bright app) and imports a per-meter cumulative kWh total
into Home Assistant via the recorder/import_statistics WebSocket command,
stamped with each hour's true time so consumption lands in the hour it
actually happened.

Design invariants — do not break these when modifying:

  * The local state file is the single source of truth for the per-resource
    frontier and cumulative total. The bridge NEVER reads statistics back
    from Home Assistant; HA is a write-only sink. Do not add
    recover-from-HA logic.
  * We supply the cumulative `sum` per hour; HA derives each hour's
    consumption by differencing consecutive sums. The cumulative is monotonic
    by construction and seeded at zero before the first backfilled hour.
  * Imports are idempotent and revisable: late data and DCC revisions are
    handled by re-importing the affected contiguous slice, recomputed from a
    known settled baseline.
  * State is written to disk before it is advanced past imported data. A
    crash mid-cycle re-imports (idempotent), never loses or double-counts.
  * Secrets (Bright password, Glow API token, HA token) must never reach the
    log stream. All log output passes through a redacting formatter.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import re
import signal
import socket
import stat
import sys
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

VERSION = "1.0.0"

GLOWMARKT_BASE_URL = "https://api.glowmarkt.com/api/v0-1"
# Application ID published by Hildebrand for the Bright app. Overridable in
# config in case Hildebrand rotate it.
DEFAULT_APPLICATION_ID = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"

HOUR = 3600  # seconds; glowbridge works in hourly statistics buckets
# Maximum span requested per readings call. The Glowmarkt API caps PT1H
# requests at 31 days, so backfill and catch-up are chunked below that.
MAX_FETCH_SPAN = timedelta(days=30)

STATE_SCHEMA = 3
STATUS_SCHEMA = 1

# Source domain for the external statistics we create in Home Assistant.
# External statistic_ids are "{STAT_SOURCE}:{object}", e.g.
# "glowbridge:electricity_consumption". ABI once history exists behind them.
STAT_SOURCE = "glowbridge"

# Max hourly rows per import_statistics message, so a year-long backfill is
# split into several sends rather than one enormous WebSocket frame.
IMPORT_BATCH = 2000

# Minimum seconds between catchup nudges per resource (the API's documented
# catchup limit). Persisted, so a restart-triggered rapid cycle cannot exceed
# it. See Bridge._maybe_catchup.
CATCHUP_FLOOR = 1800

# Consumption classifiers to bridge. Cost resources are deliberately not
# bridged: cost is derivable in Home Assistant from consumption and a tariff,
# and the API's tariff data is unreliable.
BRIDGED_CLASSIFIERS = {
    "electricity.consumption": "Electricity consumption",
    "gas.consumption": "Gas consumption",
}

log = logging.getLogger("glowbridge")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ConfigError(Exception):
    """Configuration is invalid. Fatal at startup; never caught internally."""


class CycleError(Exception):
    """A poll cycle failed after exhausting its retry budget."""


class AuthError(CycleError):
    """Authentication failed or is rate-limited by the auth floor."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class HomeAssistantConfig:
    # WebSocket URL, e.g. ws://homeassistant:8123/api/websocket. Plain ws://
    # is correct over a trusted path (Tailscale/localhost); use wss:// off an
    # untrusted network. The token is a secret: file-or-env, env wins.
    url: str = ""
    token: str = ""


@dataclasses.dataclass(frozen=True)
class GlowmarktConfig:
    username: str = ""
    password: str = ""
    application_id: str = DEFAULT_APPLICATION_ID
    resources: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ScheduleConfig:
    interval: int = 1800
    jitter: int = 300
    finalisation_lag: int = 5400
    # Fresh-install backfill window; trailing span re-checked for DCC
    # revisions each cycle; how stale a resource must be before we nudge the
    # DCC via catchup. All seconds.
    backfill_lookback: int = 31536000  # 365 days
    revision_window: int = 604800  # 7 days — covers observed multi-day DCC backfills
    catchup_stale_after: int = 86400  # 1 day


@dataclasses.dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 5
    backoff_base: int = 60
    backoff_max: int = 1800
    auth_floor: int = 3600


@dataclasses.dataclass(frozen=True)
class StateConfig:
    dir: str = ""


@dataclasses.dataclass(frozen=True)
class LoggingConfig:
    level: str = "info"
    format: str = "text"


@dataclasses.dataclass(frozen=True)
class Config:
    glowmarkt: GlowmarktConfig = dataclasses.field(default_factory=GlowmarktConfig)
    schedule: ScheduleConfig = dataclasses.field(default_factory=ScheduleConfig)
    retry: RetryConfig = dataclasses.field(default_factory=RetryConfig)
    homeassistant: HomeAssistantConfig = dataclasses.field(
        default_factory=HomeAssistantConfig
    )
    state: StateConfig = dataclasses.field(default_factory=StateConfig)
    logging: LoggingConfig = dataclasses.field(default_factory=LoggingConfig)


# Maps the TOML structure to expected value types. bool is checked before int
# because bool is a subclass of int in Python. Any key not present here is a
# fatal error: silently ignored configuration is worse than a crash.
_CONFIG_SCHEMA: dict[str, dict[str, Any]] = {
    "glowmarkt": {
        "username": str,
        "password": str,
        "application_id": str,
        "resources": list,
    },
    "schedule": {
        "interval": int,
        "jitter": int,
        "finalisation_lag": int,
        "backfill_lookback": int,
        "revision_window": int,
        "catchup_stale_after": int,
    },
    "retry": {
        "max_attempts": int,
        "backoff_base": int,
        "backoff_max": int,
        "auth_floor": int,
    },
    "homeassistant": {
        "url": str,
        "token": str,
    },
    "state": {"dir": str},
    "logging": {"level": str, "format": str},
}

ENV_GLOW_USERNAME = "GLOWBRIDGE_GLOW_USERNAME"
ENV_GLOW_PASSWORD = "GLOWBRIDGE_GLOW_PASSWORD"
ENV_HA_TOKEN = "GLOWBRIDGE_HA_TOKEN"


def _check_unknown_keys(raw: dict, schema: dict, path: str = "") -> list[str]:
    errors = []
    for key, value in raw.items():
        here = f"{path}{key}"
        if key not in schema:
            errors.append(f"unknown configuration key: {here}")
            continue
        expected = schema[key]
        if isinstance(expected, dict):
            if not isinstance(value, dict):
                errors.append(f"{here}: expected a table")
            else:
                errors.extend(_check_unknown_keys(value, expected, f"{here}."))
        else:
            if expected is int and isinstance(value, bool):
                errors.append(f"{here}: expected integer, got boolean")
            elif not isinstance(value, expected):
                errors.append(
                    f"{here}: expected {expected.__name__},"
                    f" got {type(value).__name__}"
                )
    return errors


def _validate_semantics(cfg: Config) -> list[str]:
    errors = []
    s = cfg.schedule
    if s.interval < 1800:
        # The DCC is half-hourly at best; polling faster buys nothing and
        # only adds load, so it is not supported.
        errors.append("schedule.interval: minimum is 1800 seconds (30 minutes)")
    if s.jitter < 0:
        errors.append("schedule.jitter: must be >= 0")
    if s.finalisation_lag < 0:
        errors.append("schedule.finalisation_lag: must be >= 0")
    if s.revision_window < s.finalisation_lag:
        # The trailing re-check must cover at least the un-finalised edge, or
        # a revision to a just-finalised hour would never be picked up.
        errors.append(
            "schedule.revision_window: must be >= finalisation_lag"
        )
    if s.catchup_stale_after < 0:
        errors.append("schedule.catchup_stale_after: must be >= 0")
    if s.backfill_lookback < 0:
        errors.append("schedule.backfill_lookback: must be >= 0")
    r = cfg.retry
    if r.max_attempts < 1:
        errors.append("retry.max_attempts: must be >= 1")
    if r.backoff_base < 1:
        errors.append("retry.backoff_base: must be >= 1")
    if r.backoff_max < r.backoff_base:
        errors.append("retry.backoff_max: must be >= retry.backoff_base")
    if r.auth_floor < 0:
        errors.append("retry.auth_floor: must be >= 0")
    h = cfg.homeassistant
    if not h.url:
        errors.append("homeassistant.url: required")
    elif not (h.url.startswith("ws://") or h.url.startswith("wss://")):
        errors.append("homeassistant.url: must be a ws:// or wss:// URL")
    # The HA token is deliberately NOT required here: --dry-run and --dump-raw
    # never touch HA. A missing token surfaces when the importer connects.
    g = cfg.glowmarkt
    if not g.username:
        errors.append(
            f"glowmarkt.username: required (config or {ENV_GLOW_USERNAME})"
        )
    if not g.password:
        errors.append(
            f"glowmarkt.password: required (config or {ENV_GLOW_PASSWORD})"
        )
    if not g.application_id:
        errors.append("glowmarkt.application_id: must not be empty")
    for rid in g.resources:
        if not isinstance(rid, str) or not rid:
            errors.append("glowmarkt.resources: entries must be non-empty strings")
    lvl = cfg.logging.level.lower()
    if lvl not in ("debug", "info", "warning", "error"):
        errors.append("logging.level: must be debug, info, warning or error")
    if cfg.logging.format not in ("text", "json"):
        errors.append("logging.format: must be text or json")
    return errors


def _build_section(cls, raw: dict):
    kwargs = {
        name: (tuple(value) if name == "resources" else value)
        for name, value in raw.items()
    }
    return cls(**kwargs)


def load_config(path: Path, environ: dict[str, str] | None = None) -> Config:
    """Parse, env-merge and strictly validate the TOML config at *path*."""
    environ = os.environ if environ is None else environ
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from exc

    errors = _check_unknown_keys(raw, _CONFIG_SCHEMA)
    if errors:
        raise ConfigError("; ".join(errors))

    _warn_on_readable_secrets(path, raw)

    # Env vars carry secrets only and take precedence over the file. All
    # other settings have exactly one home: the file.
    glow_raw = dict(raw.get("glowmarkt", {}))
    ha_raw = dict(raw.get("homeassistant", {}))
    if environ.get(ENV_GLOW_USERNAME):
        glow_raw["username"] = environ[ENV_GLOW_USERNAME]
    if environ.get(ENV_GLOW_PASSWORD):
        glow_raw["password"] = environ[ENV_GLOW_PASSWORD]
    if environ.get(ENV_HA_TOKEN):
        ha_raw["token"] = environ[ENV_HA_TOKEN]

    cfg = Config(
        glowmarkt=_build_section(GlowmarktConfig, glow_raw),
        schedule=_build_section(ScheduleConfig, raw.get("schedule", {})),
        retry=_build_section(RetryConfig, raw.get("retry", {})),
        homeassistant=_build_section(HomeAssistantConfig, ha_raw),
        state=_build_section(StateConfig, raw.get("state", {})),
        logging=_build_section(LoggingConfig, raw.get("logging", {})),
    )

    errors = _validate_semantics(cfg)
    if errors:
        raise ConfigError("; ".join(errors))
    return cfg


def _warn_on_readable_secrets(path: Path, raw: dict) -> None:
    """Warn if the file both contains secrets and is group/world readable.

    A config that sources its secrets from the environment is legitimately
    shareable, so the warning fires only when secrets are actually present.
    """
    glow = raw.get("glowmarkt", {})
    ha = raw.get("homeassistant", {})
    has_secrets = bool(
        glow.get("username") or glow.get("password") or ha.get("token")
    )
    if not has_secrets:
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        log.warning(
            "config file %s contains credentials and is group/world readable;"
            " chmod 600 recommended",
            path,
        )


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "glowbridge" / "glowbridge.toml"


def resolve_state_dir(cfg: Config) -> Path:
    if cfg.state.dir:
        return Path(cfg.state.dir)
    if os.environ.get("STATE_DIRECTORY"):
        # Set by systemd when the unit declares StateDirectory=.
        return Path(os.environ["STATE_DIRECTORY"].split(":")[0])
    base = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    return Path(base) / "glowbridge"


# --------------------------------------------------------------------------
# State file
# --------------------------------------------------------------------------


class StateFile:
    """Authoritative persistence: per-resource settled frontier and cumulative
    baseline, Glow auth token, discovered resource cache.

    A missing, unparseable, or unrecognised-schema file is treated as a fresh
    install, never fatal. A fresh install re-runs the backfill (seeding the
    frontier at now - backfill_lookback); because imports are idempotent this
    re-imports the same hours harmlessly rather than crash-looping. The
    on-disk layout carries a `schema` version so a format change is a clean
    version bump, not a silent misparse.
    """

    def __init__(self, state_dir: Path):
        self.path = state_dir / "state.json"
        self.data: dict[str, Any] = self._fresh()

    @staticmethod
    def _fresh() -> dict[str, Any]:
        return {
            "schema": STATE_SCHEMA,
            "auth": {"token": "", "obtained_at": "", "last_attempt_at": ""},
            "resources": {},
        }

    def load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.info("no state file at %s; starting fresh", self.path)
            return
        except OSError as exc:
            log.warning("cannot read state file %s (%s); starting fresh", self.path, exc)
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning(
                "state file %s is corrupt (%s); starting fresh", self.path, exc
            )
            return
        if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
            log.warning(
                "state file %s has unsupported schema %r; starting fresh",
                self.path,
                data.get("schema") if isinstance(data, dict) else None,
            )
            return
        merged = self._fresh()
        merged.update(data)
        self.data = merged

    def save(self) -> None:
        """Atomic write: temp file in the same directory, fsync, rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        payload = json.dumps(self.data, indent=2, sort_keys=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.path)

    # -- auth ------------------------------------------------------------

    @property
    def token(self) -> str:
        return self.data["auth"].get("token", "")

    def set_token(self, token: str, now: datetime) -> None:
        self.data["auth"]["token"] = token
        self.data["auth"]["obtained_at"] = now.isoformat()

    def clear_token(self) -> None:
        self.data["auth"]["token"] = ""

    def last_auth_attempt(self) -> datetime | None:
        return _parse_iso(self.data["auth"].get("last_attempt_at", ""))

    def mark_auth_attempt(self, now: datetime) -> None:
        self.data["auth"]["last_attempt_at"] = now.isoformat()

    # -- resources -------------------------------------------------------

    def resource_ids(self) -> list[str]:
        return list(self.data["resources"].keys())

    def resource(self, resource_id: str) -> dict[str, Any] | None:
        return self.data["resources"].get(resource_id)

    def upsert_resource(
        self, resource_id: str, classifier: str, name: str, settled_through: datetime
    ) -> None:
        if resource_id not in self.data["resources"]:
            self.data["resources"][resource_id] = {
                "classifier": classifier,
                "name": name,
                "settled_through": settled_through.isoformat(),
                "cumulative_wh_at_settled": 0,
                "data_complete_to": "",
                "last_catchup_at": "",
            }
        else:
            self.data["resources"][resource_id]["classifier"] = classifier
            self.data["resources"][resource_id]["name"] = name

    def drop_resource(self, resource_id: str) -> None:
        self.data["resources"].pop(resource_id, None)

    def settled_through(self, resource_id: str) -> datetime:
        return _parse_iso(self.data["resources"][resource_id]["settled_through"])

    def cumulative_at_settled(self, resource_id: str) -> int:
        return int(self.data["resources"][resource_id]["cumulative_wh_at_settled"])

    def data_complete_to(self, resource_id: str) -> datetime | None:
        return _parse_iso(self.data["resources"][resource_id].get("data_complete_to", ""))

    def last_catchup_at(self, resource_id: str) -> datetime | None:
        return _parse_iso(self.data["resources"][resource_id].get("last_catchup_at", ""))

    def mark_catchup(self, resource_id: str, now: datetime) -> None:
        self.data["resources"][resource_id]["last_catchup_at"] = now.isoformat()

    def commit(
        self,
        resource_id: str,
        settled_through: datetime,
        cumulative_wh_at_settled: int,
        data_complete_to: datetime | None,
    ) -> None:
        """Advance the settled frontier and its cumulative baseline.

        Both move together: cumulative_wh_at_settled is the running total as
        of settled_through, so a later cycle can recompute forward sums from a
        known point without storing per-hour energy.
        """
        entry = self.data["resources"][resource_id]
        entry["settled_through"] = settled_through.isoformat()
        entry["cumulative_wh_at_settled"] = int(cumulative_wh_at_settled)
        entry["data_complete_to"] = (
            data_complete_to.isoformat() if data_complete_to else ""
        )


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# --------------------------------------------------------------------------
# Time and scheduling
# --------------------------------------------------------------------------


def initial_settled(now: datetime, backfill_lookback: int) -> datetime:
    """Seed the settled frontier for a resource with no prior state.

    A fixed lookback (default a year) so a fresh install backfills that much
    history; hour-aligned so it lines up with HA's hourly statistics buckets.
    """
    return floor_hour(now - timedelta(seconds=backfill_lookback))


def floor_hour(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    return dt.replace(minute=0, second=0, microsecond=0)


def install_offset(seed: str, jitter: int) -> int:
    """Deterministic per-install schedule offset in [0, jitter].

    Derived from a stable machine identifier so that a given install always
    polls at the same second past the interval boundary, while the
    population of installs is spread across the jitter window. Predictable
    locally, decorrelated globally.
    """
    if jitter <= 0:
        return 0
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (jitter + 1)


def machine_seed() -> str:
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            text = Path(candidate).read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    return socket.gethostname()


def next_cycle_start(now: float, interval: int, offset: int) -> float:
    """Next poll time: interval boundaries anchored to the epoch, plus the
    per-install offset. Deliberately not now+interval: anchored boundaries
    keep poll times stable across restarts."""
    boundary = (int(now) // interval) * interval + offset
    while boundary <= now:
        boundary += interval
    return float(boundary)


# --------------------------------------------------------------------------
# Emission logic
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Reading:
    start: datetime  # hour start, UTC, hour aligned
    kwh: float


@dataclasses.dataclass(frozen=True)
class ImportPlan:
    rows: list[dict]  # finalised {"start": iso, "sum": kwh}, ascending
    new_settled_through: datetime  # frontier to commit (now - revision_window)
    new_cumulative_wh: int  # cumulative Wh through new_settled_through
    data_complete_to: datetime | None  # newest hour with real data this fetch
    imported_hours: int


def plan_import(
    readings: list[Reading],
    settled_through: datetime,
    cumulative_at_settled: int,
    now: datetime,
    finalisation_lag: int,
    revision_window: int,
) -> ImportPlan:
    """Turn a fetch window into hourly {start, sum} rows to import.

    Walks hourly from the settled frontier to now, threading the cumulative
    total (integer Wh) from the known baseline. Each present, finalised hour
    becomes a row whose ``sum`` is the cumulative *including* that hour — HA
    differences consecutive sums to get per-hour consumption, so the sum at
    hour H must be the running total through the end of H.

    HA convention (do not "fix" this into a one-hour shift): a row's ``start``
    is the BEGINNING of the hour, but its ``sum`` is the value as of the END
    of that hour. HA shows the bar at start=H as sum(H) - sum(H-1), i.e. the
    consumption during [H, H+1). Glow gives us interval-start + the energy
    used during that interval, so start=H with sum-through-end-of-H lands the
    consumption in HA's H bar exactly. Verified against HA statistics docs.

    Missing hours are skipped entirely (no row): a gap shows in HA as no
    data rather than a fabricated zero, and heals when the DCC later delivers
    it — a re-import overwrites by (statistic_id, start), so late data and
    revisions self-correct with no per-hour bookkeeping.

    The frontier only advances to ``now - revision_window``: the trailing
    window stays re-fetchable so DCC revisions and late fills self-correct.
    The cumulative baseline for that point is captured as the walk crosses
    it, so the next cycle recomputes forward sums from a known base without
    storing per-hour energy.
    """
    step = timedelta(seconds=HOUR)
    cutoff = now - timedelta(seconds=finalisation_lag)
    settle_target = floor_hour(now - timedelta(seconds=revision_window))
    if settle_target < settled_through:
        settle_target = settled_through  # never regress the frontier

    by_start = {r.start: r.kwh for r in readings if r.start >= settled_through}
    newest_present = max(by_start) if by_start else None

    cum = cumulative_at_settled
    cum_at_target = cumulative_at_settled
    hit_target = False
    rows: list[dict] = []
    cursor = settled_through
    while cursor < now:
        if cursor == settle_target:
            # Cumulative through settle_target: this hour not yet added, so
            # cum is the sum of all hours strictly before the new frontier.
            cum_at_target = cum
            hit_target = True
        kwh = by_start.get(cursor)
        if kwh is not None:
            cum += round(kwh * 1000)
            if cursor + step <= cutoff:  # finalised
                rows.append(
                    {"start": cursor.isoformat(), "sum": round(cum / 1000, 3)}
                )
        cursor += step

    if not hit_target:
        # settle_target sits at or beyond `now` (e.g. a tiny revision_window
        # with an hour-aligned now): everything walked is before the frontier,
        # so the cumulative through it is the final running total.
        cum_at_target = cum

    return ImportPlan(
        rows=rows,
        new_settled_through=settle_target,
        new_cumulative_wh=cum_at_target,
        data_complete_to=newest_present,
        imported_hours=len(rows),
    )


def fetch_windows(
    start: datetime, end: datetime, span: timedelta = MAX_FETCH_SPAN
) -> list[tuple[datetime, datetime]]:
    """Split [start, end) into API-sized request windows."""
    windows = []
    cursor = start
    while cursor < end:
        windows.append((cursor, min(cursor + span, end)))
        cursor = min(cursor + span, end)
    return windows


# --------------------------------------------------------------------------
# Glowmarkt API client
# --------------------------------------------------------------------------


class GlowmarktClient:
    """Thin client for the Glowmarkt v0-1 REST API.

    Timeouts are deliberately aggressive: this API is known to hang, and a
    hung request blocks the whole cycle. All calls raise
    requests.RequestException or CycleError; retry policy lives in the
    cycle runner, not here.
    """

    CONNECT_TIMEOUT = 10
    READ_TIMEOUT = 30
    CATCHUP_TIMEOUT = (5, 10)

    def __init__(self, cfg: GlowmarktConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "applicationId": cfg.application_id,
                "Content-Type": "application/json",
                "User-Agent": f"glowbridge/{VERSION}",
            }
        )
        self.token = ""

    def set_token(self, token: str) -> None:
        self.token = token
        if token:
            self.session.headers["token"] = token
        else:
            self.session.headers.pop("token", None)

    def authenticate(self) -> str:
        resp = self.session.post(
            f"{GLOWMARKT_BASE_URL}/auth",
            json={"username": self.cfg.username, "password": self.cfg.password},
            timeout=(self.CONNECT_TIMEOUT, self.READ_TIMEOUT),
        )
        if resp.status_code in (401, 403):
            raise AuthError(f"authentication rejected (HTTP {resp.status_code})")
        resp.raise_for_status()
        body = resp.json()
        token = body.get("token", "")
        if not token or body.get("valid") is False:
            raise AuthError("authentication response contained no valid token")
        self.set_token(token)
        return token

    def get_resources(self) -> list[dict[str, Any]]:
        resp = self._get(f"{GLOWMARKT_BASE_URL}/resource")
        body = resp.json()
        if not isinstance(body, list):
            raise CycleError("unexpected /resource response shape")
        return body

    def catchup(self, resource_id: str) -> None:
        """Nudge the DCC pipeline to refresh this resource. Best effort:
        this endpoint times out routinely and a failure here does not
        affect what /readings returns for already-delivered data."""
        try:
            self.session.get(
                f"{GLOWMARKT_BASE_URL}/resource/{resource_id}/catchup",
                timeout=self.CATCHUP_TIMEOUT,
            )
        except requests.RequestException as exc:
            log.debug("catchup for %s failed (ignored): %s", resource_id, exc)

    def get_readings(
        self, resource_id: str, start: datetime, end: datetime
    ) -> list[Reading]:
        params = {
            "from": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "to": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "period": "PT1H",
            "function": "sum",
            "offset": 0,  # UTC — aligns hour boundaries with HA's statistics
            "nulls": 1,
        }
        resp = self._get(
            f"{GLOWMARKT_BASE_URL}/resource/{resource_id}/readings", params=params
        )
        body = resp.json()
        rows = body.get("data")
        if not isinstance(rows, list):
            raise CycleError(
                f"unexpected /readings response shape for {resource_id}"
            )
        readings = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 2:
                continue
            ts, value = row[0], row[1]
            if value is None:
                continue
            readings.append(
                Reading(
                    start=datetime.fromtimestamp(int(ts), tz=UTC),
                    kwh=float(value),
                )
            )
        readings.sort(key=lambda r: r.start)
        return readings

    def get_readings_raw(
        self, resource_id: str, start: datetime, end: datetime
    ) -> tuple[list, dict]:
        """Raw /readings for --dump-raw: the untransformed data rows (nulls
        included) plus the response envelope with data stripped, for
        eyeballing the API's actual shape. No transform, no null filtering."""
        params = {
            "from": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "to": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "period": "PT1H",
            "function": "sum",
            "offset": 0,
            "nulls": 1,
        }
        resp = self._get(
            f"{GLOWMARKT_BASE_URL}/resource/{resource_id}/readings", params=params
        )
        body = resp.json()
        rows = body.get("data")
        if not isinstance(rows, list):
            rows = []
        envelope = {k: v for k, v in body.items() if k != "data"}
        return rows, envelope

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        resp = self.session.get(
            url, params=params, timeout=(self.CONNECT_TIMEOUT, self.READ_TIMEOUT)
        )
        if resp.status_code == 401:
            raise TokenExpired()
        if resp.status_code == 404:
            raise ResourceMissing(url)
        if resp.status_code == 429:
            raise RateLimited(_retry_after_seconds(resp))
        resp.raise_for_status()
        return resp


class TokenExpired(Exception):
    pass


class ResourceMissing(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after: int | None):
        super().__init__(f"rate limited (Retry-After: {retry_after})")
        self.retry_after = retry_after


def _retry_after_seconds(resp: requests.Response) -> int | None:
    value = resp.headers.get("Retry-After", "")
    try:
        return max(0, int(value))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Home Assistant statistics importer
# --------------------------------------------------------------------------


def statistic_id_for(resource_id: str, classifier: str) -> str:
    """External statistic_id for a resource.

    Classifier-based for discovered consumption resources (stable, readable):
    "electricity.consumption" -> "glowbridge:electricity_consumption". For a
    pinned resource, whose classifier we never look up, it falls back to the
    resource id so each pinned resource still gets a distinct, stable id.
    ABI once history exists behind it: the `:` marks it external (our own).
    """
    if classifier in BRIDGED_CLASSIFIERS:
        obj = classifier.replace(".", "_")
    else:
        obj = resource_id.replace("-", "_")
    return f"{STAT_SOURCE}:{obj}"


class HaImporter:
    """Write-only Home Assistant long-term statistics importer.

    Opens one WebSocket per cycle, authenticates with a long-lived token, and
    sends recorder/import_statistics commands. Reads nothing back beyond
    command acknowledgements — HA is a pure sink. Lifecycle per cycle:
    connect() → import_statistics() one or more times → close().
    """

    CONNECT_TIMEOUT = 15
    RECV_TIMEOUT = 30

    def __init__(self, cfg: HomeAssistantConfig):
        import websocket  # lazy import; only needed when actually importing

        self._websocket = websocket
        self.cfg = cfg
        self.ws = None
        self._id = 0

    def connect(self) -> None:
        if not self.cfg.token:
            # AuthError so the cycle is abandoned, not retried in a loop: a
            # missing/invalid token will not fix itself within a cycle.
            raise AuthError(
                f"Home Assistant token not set ({ENV_HA_TOKEN}); cannot import"
            )
        try:
            self.ws = self._websocket.create_connection(
                self.cfg.url, timeout=self.CONNECT_TIMEOUT
            )
            self.ws.settimeout(self.RECV_TIMEOUT)
        except Exception as exc:
            raise CycleError(
                f"cannot connect to Home Assistant at {self.cfg.url}: {exc}"
            ) from exc
        hello = self._recv()
        if hello.get("type") != "auth_required":
            raise CycleError(
                f"unexpected Home Assistant greeting: {hello.get('type')!r}"
            )
        self._send({"type": "auth", "access_token": self.cfg.token})
        result = self._recv()
        if result.get("type") != "auth_ok":
            raise AuthError("Home Assistant rejected the access token")

    def import_statistics(
        self, statistic_id: str, name: str, stats: list[dict]
    ) -> None:
        """Import one contiguous run of hourly {start, sum} rows.

        The metadata block creates the external statistic on first import;
        subsequent imports overwrite by (statistic_id, start), which is how
        revisions and late gap-fills self-correct.
        """
        self._id += 1
        self._send(
            {
                "id": self._id,
                "type": "recorder/import_statistics",
                "metadata": {
                    "has_mean": False,
                    "has_sum": True,
                    "name": name,
                    "source": STAT_SOURCE,
                    "statistic_id": statistic_id,
                    "unit_of_measurement": "kWh",
                },
                "stats": stats,
            }
        )
        result = self._recv()
        if not result.get("success"):
            raise CycleError(
                f"Home Assistant rejected import for {statistic_id}:"
                f" {result.get('error')}"
            )

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def _send(self, msg: dict) -> None:
        self.ws.send(json.dumps(msg))

    def _recv(self) -> dict:
        return json.loads(self.ws.recv())


# --------------------------------------------------------------------------
# Cycle runner
# --------------------------------------------------------------------------


class Bridge:
    def __init__(
        self,
        cfg: Config,
        state: StateFile,
        client: GlowmarktClient,
        importer: HaImporter | None,
    ):
        self.cfg = cfg
        self.state = state
        self.client = client
        self.importer = importer  # None in --dry-run / --dump-raw
        self.consecutive_failures = 0
        self.last_success: datetime | None = None
        self.last_attempt: datetime | None = None
        self.last_error = ""
        # Secrets scrubbed from the status file's last_error, same set the
        # log formatter redacts.
        self._secrets = [cfg.glowmarkt.password, cfg.homeassistant.token]

    @property
    def status_path(self) -> Path:
        return self.state.path.parent / "status.json"

    def _seed(self, now: datetime) -> datetime:
        return initial_settled(now, self.cfg.schedule.backfill_lookback)

    # -- auth ------------------------------------------------------------

    def ensure_token(self, now: datetime, force: bool = False) -> None:
        """Authenticate if there is no cached token (or *force*), subject to
        the auth floor.

        The floor is an account-lockout guard: Hildebrand rate-limit and
        have locked accounts over aggressive re-auth. It applies across
        restarts (the last attempt time is persisted), so a crash loop
        cannot become an auth loop.
        """
        if self.state.token and not force:
            self.client.set_token(self.state.token)
            return
        last = self.state.last_auth_attempt()
        floor = timedelta(seconds=self.cfg.retry.auth_floor)
        if last is not None and now - last < floor:
            wait = (last + floor) - now
            raise AuthError(
                f"auth needed but floor not met; retry in {int(wait.total_seconds())}s"
            )
        self.state.mark_auth_attempt(now)
        self.state.save()
        token = self.client.authenticate()
        self.state.set_token(token, now)
        self.state.save()
        log.info("authenticated with Glowmarkt")

    # -- resources -------------------------------------------------------

    def resolve_resources(self, now: datetime) -> list[str]:
        """Return the resource IDs to poll this cycle.

        Pinned config resources are authoritative and skip discovery
        entirely. Otherwise the state-file cache is used, populated by
        discovery on first run and refreshed by rediscover() when a cached
        resource disappears from the API.
        """
        if self.cfg.glowmarkt.resources:
            for rid in self.cfg.glowmarkt.resources:
                if self.state.resource(rid) is None:
                    self.state.upsert_resource(
                        rid, "pinned", rid, self._seed(now)
                    )
            return list(self.cfg.glowmarkt.resources)
        cached = self.state.resource_ids()
        if cached:
            return cached
        return self.discover(now)

    def discover(self, now: datetime) -> list[str]:
        found = []
        for res in self.client.get_resources():
            classifier = res.get("classifier", "")
            rid = res.get("resourceId", "")
            if not rid or classifier not in BRIDGED_CLASSIFIERS:
                continue
            name = res.get("name") or BRIDGED_CLASSIFIERS[classifier]
            self.state.upsert_resource(
                rid, classifier, name, self._seed(now)
            )
            found.append(rid)
        if not found:
            raise CycleError(
                "discovery found no consumption resources; check that the"
                " Bright account has meters attached and data visible"
            )
        log.info("discovered %d consumption resource(s): %s", len(found), found)
        self.state.save()
        return found

    def rediscover(self, missing_id: str, now: datetime) -> None:
        """A cached resource 404ed. Supplier switches and meter exchanges
        mint new resource IDs, so drop it and re-run discovery. A pinned
        resource that 404s is a hard error instead: the user asserted the
        ID, so silently substituting a different one would be wrong."""
        if self.cfg.glowmarkt.resources:
            raise CycleError(
                f"pinned resource {missing_id} does not exist (HTTP 404);"
                " fix glowmarkt.resources in config"
            )
        log.warning(
            "resource %s no longer exists; dropping and rediscovering",
            missing_id,
        )
        self.state.drop_resource(missing_id)
        self.discover(now)

    # -- one cycle -------------------------------------------------------

    def run_cycle(self, now: datetime) -> None:
        """One poll: per resource, fetch hourly readings, import the finalised
        rows to Home Assistant, then advance the settled frontier.

        Ordering is load-bearing: import FIRST, then commit the frontier.
        Imports are idempotent, so a crash after importing but before
        committing simply re-imports next cycle; committing the frontier
        first would risk skipping un-imported data.
        """
        self.last_attempt = now
        self.ensure_token(now)
        resources = self.resolve_resources(now)

        imported_hours = 0
        try:
            for rid in resources:
                try:
                    imported_hours += self._process_resource(rid, now)
                except TokenExpired:
                    log.info("token rejected; re-authenticating")
                    self.state.clear_token()
                    self.client.set_token("")
                    self.ensure_token(now, force=True)
                    imported_hours += self._process_resource(rid, now)
                except ResourceMissing:
                    self.rediscover(rid, now)
                    # Replacement resources are polled next cycle; this cycle
                    # continues with the survivors.
        finally:
            # One WebSocket per cycle: opened lazily on first import, always
            # closed here.
            if self.importer is not None:
                self.importer.close()

        self.last_success = now
        self.consecutive_failures = 0
        self.last_error = ""
        log.info(
            "cycle complete: %d resource(s), %d hour(s) imported",
            len(resources),
            imported_hours,
        )
        # importer is None only in --dry-run, which writes nothing.
        if self.importer is not None:
            self.write_status(now)

    def _process_resource(self, rid: str, now: datetime) -> int:
        """Fetch, import and advance one resource. Returns hours imported."""
        entry = self.state.resource(rid) or {}
        classifier = entry.get("classifier", "")
        settled = floor_hour(self.state.settled_through(rid))
        cumulative = self.state.cumulative_at_settled(rid)

        self._maybe_catchup(rid, now)

        readings: list[Reading] = []
        # Re-fetch the whole [frontier, now] span every cycle: the trailing
        # revision window is re-imported so DCC revisions and late fills
        # self-correct via idempotent overwrite.
        for start, end in fetch_windows(settled, now):
            readings.extend(self.client.get_readings(rid, start, end))

        plan = plan_import(
            readings,
            settled,
            cumulative,
            now,
            self.cfg.schedule.finalisation_lag,
            self.cfg.schedule.revision_window,
        )

        if self.importer is not None and plan.rows:
            self._import_rows(
                statistic_id_for(rid, classifier),
                entry.get("name") or classifier or rid,
                plan.rows,
            )

        # data_complete_to never regresses: keep the newest real hour ever seen.
        newest = plan.data_complete_to
        prev = self.state.data_complete_to(rid)
        if prev is not None and (newest is None or prev > newest):
            newest = prev

        # Commit AFTER importing (see run_cycle): a crash here re-imports.
        self.state.commit(
            rid, plan.new_settled_through, plan.new_cumulative_wh, newest
        )
        self.state.save()

        log.debug(
            "resource %s (%s): imported %d hour(s), frontier -> %s,"
            " data complete to %s",
            rid,
            classifier or "?",
            plan.imported_hours,
            plan.new_settled_through.isoformat(),
            newest.isoformat() if newest else "never",
        )
        if self.importer is None:
            self._print_dry_run(rid, classifier, plan)
        return plan.imported_hours

    def _maybe_catchup(self, rid: str, now: datetime) -> None:
        """Nudge the DCC via catchup only when a resource is stale by more
        than catchup_stale_after, throttled to once per CATCHUP_FLOOR and
        persisted so a restart cannot exceed it. Best effort; skipped in
        dry-run (importer is None)."""
        if self.importer is None:
            return
        complete = self.state.data_complete_to(rid)
        if complete is None:
            return  # fresh resource: the backfill fetches whatever exists
        if now - complete <= timedelta(seconds=self.cfg.schedule.catchup_stale_after):
            return
        last = self.state.last_catchup_at(rid)
        if last is not None and now - last < timedelta(seconds=CATCHUP_FLOOR):
            return
        log.info(
            "resource %s stale since %s; nudging the DCC via catchup",
            rid,
            complete.isoformat(),
        )
        self.state.mark_catchup(rid, now)
        self.state.save()
        self.client.catchup(rid)

    def _import_rows(self, statistic_id: str, name: str, rows: list[dict]) -> None:
        if self.importer.ws is None:
            self.importer.connect()  # lazy: one connection per cycle
        for i in range(0, len(rows), IMPORT_BATCH):
            self.importer.import_statistics(
                statistic_id, name, rows[i : i + IMPORT_BATCH]
            )

    def next_planned(self, now: datetime) -> datetime:
        ts = next_cycle_start(
            now.timestamp(),
            self.cfg.schedule.interval,
            install_offset(machine_seed(), self.cfg.schedule.jitter),
        )
        return datetime.fromtimestamp(ts, tz=UTC)

    def write_status(
        self, now: datetime, next_planned: datetime | None = None
    ) -> None:
        """Rewrite the observability status file next to state.json. Safe to
        paste into a bug report — last_error is redacted, no secrets, and the
        auth token lives only in state.json, never here. Never fatal: a status
        write failure must not fail a cycle."""
        payload: dict[str, Any] = {
            "schema": STATUS_SCHEMA,
            "bridge_version": VERSION,
            "last_attempt": _iso_or_empty(self.last_attempt),
            "last_success": _iso_or_empty(self.last_success),
            "consecutive_failures": self.consecutive_failures,
            "next_planned_update": (
                next_planned or self.next_planned(now)
            ).isoformat(),
            "last_error": redact(self.last_error, self._secrets),
            "resources": {},
        }
        for rid in self.state.resource_ids():
            entry = self.state.resource(rid) or {}
            classifier = entry.get("classifier", "")
            payload["resources"][statistic_id_for(rid, classifier)] = {
                "resource_id": rid,
                "classifier": classifier,
                "settled_through": entry.get("settled_through", ""),
                "data_complete_to": entry.get("data_complete_to", ""),
                "cumulative_kwh": round(
                    int(entry.get("cumulative_wh_at_settled", 0)) / 1000, 3
                ),
                "last_catchup_at": entry.get("last_catchup_at", ""),
            }
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
            )
            os.replace(tmp, self.status_path)
        except OSError as exc:
            log.warning("could not write status file %s: %s", self.status_path, exc)

    def _print_dry_run(self, rid: str, classifier: str, plan: ImportPlan) -> None:
        sid = statistic_id_for(rid, classifier)
        span = ""
        if plan.rows:
            span = (
                f" [{plan.rows[0]['start']} sum={plan.rows[0]['sum']}"
                f" .. {plan.rows[-1]['start']} sum={plan.rows[-1]['sum']}]"
            )
        print(
            f"[dry-run] {sid}: {plan.imported_hours} hour(s) to import{span};"
            f" frontier -> {plan.new_settled_through.isoformat()}"
        )


def _iso_or_empty(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


# --------------------------------------------------------------------------
# Retry orchestration
# --------------------------------------------------------------------------


def run_cycle_with_retries(bridge: Bridge, cfg: Config, sleeper=time.sleep) -> bool:
    """Run one cycle with in-cycle retries; returns success.

    Exhausting the retry budget is not fatal to the daemon: the cycle is
    skipped and the next one runs on schedule. Retry-After from a 429 is
    honoured in preference to computed backoff.
    """
    retry = cfg.retry
    for attempt in range(1, retry.max_attempts + 1):
        try:
            bridge.run_cycle(datetime.now(tz=UTC))
            return True
        except AuthError as exc:
            # Retrying inside the cycle cannot help before the floor passes.
            log.error("cycle abandoned: %s", exc)
            bridge.last_error = str(exc)
            break
        except RateLimited as exc:
            bridge.last_error = str(exc)
            delay = exc.retry_after or _backoff_delay(retry, attempt)
            log.warning(
                "rate limited (attempt %d/%d); backing off %ds",
                attempt,
                retry.max_attempts,
                delay,
            )
            if attempt < retry.max_attempts:
                sleeper(delay)
        except (requests.RequestException, CycleError) as exc:
            bridge.last_error = str(exc)
            delay = _backoff_delay(retry, attempt)
            log.warning(
                "cycle attempt %d/%d failed: %s",
                attempt,
                retry.max_attempts,
                exc,
            )
            if attempt < retry.max_attempts:
                sleeper(delay)
    bridge.consecutive_failures += 1
    return False


def _backoff_delay(retry: RetryConfig, attempt: int) -> int:
    base = retry.backoff_base * (2 ** (attempt - 1))
    capped = min(base, retry.backoff_max)
    # Full jitter: decorrelates retries across installs that failed on the
    # same upstream incident.
    return 1 + int.from_bytes(os.urandom(2), "big") % max(capped, 1)


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


# Masks token/password/access_token JSON fields, so a dumped raw API
# response or WebSocket frame cannot leak a credential even if the value is
# not in the known-secrets list.
_REDACT_PATTERNS = [
    re.compile(
        r'("(?:token|password|access_token)"\s*:\s*")[^"]*(")', re.IGNORECASE
    ),
]


def redact(text: str, secrets: list[str]) -> str:
    """Scrub known secret values wholesale, then mask credential-ish JSON
    fields. Shared by the log formatter and the status file's last_error so
    both are safe to paste into a bug report."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub(r"\1***\2", text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formats then scrubs, so a debug dump of a raw API response cannot leak
    credentials into a pasted log."""

    def __init__(self, fmt: str, secrets: list[str], json_mode: bool = False):
        super().__init__(fmt)
        self.secrets = [s for s in secrets if s]
        self.json_mode = json_mode

    def format(self, record: logging.LogRecord) -> str:
        if self.json_mode:
            rendered = json.dumps(
                {
                    "ts": datetime.now(tz=UTC).isoformat(),
                    "level": record.levelname.lower(),
                    "logger": record.name,
                    "msg": record.getMessage(),
                },
                sort_keys=True,
            )
        else:
            rendered = super().format(record)
        return redact(rendered, self.secrets)


def setup_logging(cfg: Config, debug_override: bool) -> None:
    level = "debug" if debug_override else cfg.logging.level
    handler = logging.StreamHandler(sys.stderr)
    secrets = [
        cfg.glowmarkt.password,
        cfg.homeassistant.token,
    ]
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            secrets=secrets,
            json_mode=cfg.logging.format == "json",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # urllib3 and websocket-client are chatty at DEBUG and their payload
    # logging bypasses this formatter's secret list; keep them at INFO.
    logging.getLogger("urllib3").setLevel(logging.INFO)
    logging.getLogger("websocket").setLevel(logging.INFO)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


class Shutdown:
    def __init__(self):
        self.requested = False

    def install(self) -> None:
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, signum, frame) -> None:
        log.info("received signal %d; shutting down", signum)
        self.requested = True

    def sleep_until(self, deadline: float) -> None:
        while not self.requested and time.time() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.time())))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glowbridge",
        description="Import Glowmarkt/DCC smart meter readings into Home"
        " Assistant long-term statistics.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"config file path (default: {default_config_path()})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single poll cycle and exit; non-zero exit on failure",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and print what would be imported; no HA import, no state writes",
    )
    parser.add_argument(
        "--dump-raw",
        action="store_true",
        help="dump raw API rows (nulls included) to JSON for inspection;"
        " no HA import, no state writes",
    )
    parser.add_argument(
        "--from",
        dest="dump_from",
        default=None,
        help="--dump-raw start timestamp, ISO (default: the backfill window)",
    )
    parser.add_argument(
        "--to",
        dest="dump_to",
        default=None,
        help="--dump-raw end timestamp, ISO (default: now)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="--dump-raw output file (default: glow_dump.json in the state dir)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="force debug logging (secret redaction stays active)",
    )
    parser.add_argument(
        "--version", action="version", version=f"glowbridge {VERSION}"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config_path = args.config or default_config_path()

    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"glowbridge: configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg, args.debug)

    state_dir = resolve_state_dir(cfg)
    state = StateFile(state_dir)
    state.load()

    client = GlowmarktClient(cfg.glowmarkt)

    if args.dump_raw:
        return dump_raw(cfg, client, state, args, state_dir)

    # --dry-run imports nothing and writes no state; note the missing token
    # so it's clear a real run would need one.
    no_side_effects = args.dry_run
    importer: HaImporter | None = None
    if no_side_effects:
        if not cfg.homeassistant.token:
            log.info(
                "no Home Assistant token set (%s); --dry-run does not import",
                ENV_HA_TOKEN,
            )
    else:
        importer = HaImporter(cfg.homeassistant)

    bridge = Bridge(cfg, state, client, importer)

    if no_side_effects:
        # Dry runs must not mutate persistent state.
        state.save = lambda: None  # type: ignore[method-assign]

    if args.once or args.dry_run:
        ok = run_cycle_with_retries(bridge, cfg)
        if not ok and importer is not None:
            bridge.write_status(datetime.now(tz=UTC))
        return 0 if ok else 1
    return _daemon_loop(bridge, cfg)


def dump_raw(
    cfg: Config,
    client: GlowmarktClient,
    state: StateFile,
    args: argparse.Namespace,
    state_dir: Path,
) -> int:
    """Fetch raw, untransformed API rows (nulls included) and write them to
    JSON for inspection. No HA import, no state writes — the debugging tool
    that replaces the old dev_scripts/dump_readings.py."""
    if not cfg.homeassistant.token:
        log.info("no Home Assistant token set (%s); --dump-raw does not import", ENV_HA_TOKEN)
    state.save = lambda: None  # type: ignore[method-assign]
    bridge = Bridge(cfg, state, client, None)
    now = datetime.now(tz=UTC)
    try:
        bridge.ensure_token(now)
        resources = bridge.resolve_resources(now)
    except (requests.RequestException, CycleError) as exc:
        log.error("dump-raw failed: %s", exc)
        return 1

    start = _parse_iso(args.dump_from) if args.dump_from else bridge._seed(now)
    end = _parse_iso(args.dump_to) if args.dump_to else now
    output = args.output or (state_dir / "glow_dump.json")

    out: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "base_url": GLOWMARKT_BASE_URL,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "resources": {},
    }
    for rid in resources:
        rows_by_ts: dict[int, list] = {}
        envelope: dict = {}
        for a, b in fetch_windows(start, end):
            rows, env = client.get_readings_raw(rid, a, b)
            envelope = envelope or env
            for row in rows:
                if isinstance(row, list) and row:
                    rows_by_ts[row[0]] = row
        readings = [rows_by_ts[ts] for ts in sorted(rows_by_ts)]
        out["resources"][rid] = {
            "response_meta": envelope,
            "reading_count": len(readings),
            "null_count": sum(1 for r in readings if len(r) < 2 or r[1] is None),
            "readings": readings,
        }
        log.info("%s: %d raw row(s)", rid, len(readings))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"dump-raw: wrote {len(out['resources'])} resource(s) to {output}")
    return 0


def _daemon_loop(bridge: Bridge, cfg: Config) -> int:
    shutdown = Shutdown()
    shutdown.install()
    offset = install_offset(machine_seed(), cfg.schedule.jitter)
    log.info(
        "glowbridge %s starting: interval %ds, install offset %ds,"
        " finalisation lag %ds",
        VERSION,
        cfg.schedule.interval,
        offset,
        cfg.schedule.finalisation_lag,
    )

    while not shutdown.requested:
        ok = run_cycle_with_retries(bridge, cfg)
        now = datetime.now(tz=UTC)
        next_ts = next_cycle_start(time.time(), cfg.schedule.interval, offset)
        if not ok:
            # A successful cycle writes status itself; a failed one is
            # written here with the pushed-out next_planned_update.
            bridge.write_status(now, datetime.fromtimestamp(next_ts, tz=UTC))
        log.info(
            "next cycle at %s",
            datetime.fromtimestamp(next_ts, tz=UTC).isoformat(),
        )
        shutdown.sleep_until(next_ts)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
