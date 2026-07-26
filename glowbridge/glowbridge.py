#!/usr/bin/env -S uv run --script
# SPDX-License-Identifier: MIT
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests>=2.31",
#     "paho-mqtt>=2.0",
# ]
# ///
"""glowbridge: publish UK smart meter readings from the Glowmarkt/DCC API to MQTT.

Fetches finalised half-hourly consumption intervals from the Glowmarkt API
(the backend behind Hildebrand's Bright app) and publishes a monotonic
cumulative kWh total per meter to MQTT, with Home Assistant discovery.

Design invariants — do not break these when modifying:

  * The local state file is the single source of truth for the watermark and
    cumulative total. The broker copy (retained status topics) is
    observability only. The bridge NEVER subscribes to anything; it is a
    pure publisher. Do not add recover-from-broker logic.
  * Published cumulative values are monotonic by construction. Only intervals
    past the finalisation lag are emitted, and each interval is emitted
    exactly once. Emission is tracked by a contiguous frontier (watermark)
    plus a ledger of already-emitted windows ahead of it: a gap holds the
    frontier (so data_complete_to stays honest) without blocking emission of
    later windows, and a late arrival heals the gap exactly once. See
    select_emittable.
  * State is written to disk before anything is published. A crash between
    write and publish loses one publish (a gap), never double-counts.
  * Secrets (Bright password, API token, MQTT password) must never reach the
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

VERSION = "0.1.0"

GLOWMARKT_BASE_URL = "https://api.glowmarkt.com/api/v0-1"
# Application ID published by Hildebrand for the Bright app. Overridable in
# config in case Hildebrand rotate it.
DEFAULT_APPLICATION_ID = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"

HALF_HOUR = 1800  # seconds; the DCC reporting interval
# How far back a resource with no prior state seeds its watermark.
INITIAL_BACKFILL = timedelta(hours=24)
# Maximum span requested per readings call. The API rejects or truncates
# large PT30M ranges, so catch-up after downtime is chunked.
MAX_FETCH_SPAN = timedelta(days=7)

STATE_SCHEMA = 2
STATUS_SCHEMA = 1

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
class TlsConfig:
    enabled: bool = False
    ca_cert: str = ""
    client_cert: str = ""
    client_key: str = ""
    insecure_skip_verify: bool = False


@dataclasses.dataclass(frozen=True)
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    client_id: str = "glowbridge"
    topic_prefix: str = "glowbridge"
    discovery_prefix: str = "homeassistant"
    qos: int = 1
    tls: TlsConfig = dataclasses.field(default_factory=TlsConfig)


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
    heal_horizon: int = 604800


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
    mqtt: MqttConfig = dataclasses.field(default_factory=MqttConfig)
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
        "heal_horizon": int,
    },
    "retry": {
        "max_attempts": int,
        "backoff_base": int,
        "backoff_max": int,
        "auth_floor": int,
    },
    "mqtt": {
        "host": str,
        "port": int,
        "username": str,
        "password": str,
        "client_id": str,
        "topic_prefix": str,
        "discovery_prefix": str,
        "qos": int,
        "tls": {
            "enabled": bool,
            "ca_cert": str,
            "client_cert": str,
            "client_key": str,
            "insecure_skip_verify": bool,
        },
    },
    "state": {"dir": str},
    "logging": {"level": str, "format": str},
}

ENV_GLOW_USERNAME = "GLOWBRIDGE_GLOW_USERNAME"
ENV_GLOW_PASSWORD = "GLOWBRIDGE_GLOW_PASSWORD"
ENV_MQTT_PASSWORD = "GLOWBRIDGE_MQTT_PASSWORD"


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
    if s.interval < 300:
        errors.append("schedule.interval: minimum is 300 seconds")
    if s.jitter < 0:
        errors.append("schedule.jitter: must be >= 0")
    if s.finalisation_lag < 0:
        errors.append("schedule.finalisation_lag: must be >= 0")
    if s.heal_horizon <= s.finalisation_lag:
        # A gap must outlive the finalisation lag before it can be abandoned,
        # otherwise the frontier could skip a window that has not yet had its
        # chance to arrive.
        errors.append(
            "schedule.heal_horizon: must be greater than finalisation_lag"
        )
    r = cfg.retry
    if r.max_attempts < 1:
        errors.append("retry.max_attempts: must be >= 1")
    if r.backoff_base < 1:
        errors.append("retry.backoff_base: must be >= 1")
    if r.backoff_max < r.backoff_base:
        errors.append("retry.backoff_max: must be >= retry.backoff_base")
    if r.auth_floor < 0:
        errors.append("retry.auth_floor: must be >= 0")
    m = cfg.mqtt
    if not (1 <= m.port <= 65535):
        errors.append("mqtt.port: must be 1-65535")
    if m.qos not in (0, 1):
        errors.append("mqtt.qos: must be 0 or 1")
    if not m.host:
        errors.append("mqtt.host: must not be empty")
    if not m.topic_prefix or "#" in m.topic_prefix or "+" in m.topic_prefix:
        errors.append("mqtt.topic_prefix: must be non-empty, no wildcards")
    t = m.tls
    if bool(t.client_cert) != bool(t.client_key):
        errors.append(
            "mqtt.tls: client_cert and client_key must be set together"
        )
    if (t.ca_cert or t.client_cert or t.insecure_skip_verify) and not t.enabled:
        errors.append("mqtt.tls: options set but tls.enabled is false")
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
    # Deferred annotations mean dataclass field types arrive as strings, so
    # nested tables are dispatched by name, not by type inspection.
    _NESTED = {"tls": TlsConfig}
    kwargs = {}
    for name, value in raw.items():
        if name in _NESTED and isinstance(value, dict):
            kwargs[name] = _build_section(_NESTED[name], value)
        elif name == "resources":
            kwargs[name] = tuple(value)
        else:
            kwargs[name] = value
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
    mqtt_raw = dict(raw.get("mqtt", {}))
    if environ.get(ENV_GLOW_USERNAME):
        glow_raw["username"] = environ[ENV_GLOW_USERNAME]
    if environ.get(ENV_GLOW_PASSWORD):
        glow_raw["password"] = environ[ENV_GLOW_PASSWORD]
    if environ.get(ENV_MQTT_PASSWORD):
        mqtt_raw["password"] = environ[ENV_MQTT_PASSWORD]

    cfg = Config(
        glowmarkt=_build_section(GlowmarktConfig, glow_raw),
        schedule=_build_section(ScheduleConfig, raw.get("schedule", {})),
        retry=_build_section(RetryConfig, raw.get("retry", {})),
        mqtt=_build_section(MqttConfig, mqtt_raw),
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
    mqtt = raw.get("mqtt", {})
    has_secrets = bool(
        glow.get("username") or glow.get("password") or mqtt.get("password")
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
    """Authoritative persistence: watermarks, cumulative totals, auth token,
    discovered resource cache.

    A missing or unparseable file is treated as a fresh install, never as a
    fatal error; recovery behaviour for that case lives in the emission
    logic (cumulative restarts from 24 hours before the current run,
    producing one plausible meter-reset event downstream rather than a
    crash loop).
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
        if isinstance(data, dict):
            data = _migrate_state(data)
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
        self, resource_id: str, classifier: str, name: str, watermark: datetime
    ) -> None:
        if resource_id not in self.data["resources"]:
            self.data["resources"][resource_id] = {
                "classifier": classifier,
                "name": name,
                "watermark": watermark.isoformat(),
                "emitted": [],
                "cumulative_wh": 0,
            }
        else:
            self.data["resources"][resource_id]["classifier"] = classifier
            self.data["resources"][resource_id]["name"] = name

    def drop_resource(self, resource_id: str) -> None:
        self.data["resources"].pop(resource_id, None)

    def watermark(self, resource_id: str) -> datetime:
        return _parse_iso(self.data["resources"][resource_id]["watermark"])

    def emitted(self, resource_id: str) -> set[datetime]:
        """Starts of already-emitted windows lying ahead of the watermark.

        These are the non-contiguous windows folded into the cumulative
        total while a gap below them still holds the frontier back. Held as
        a set so re-fetched windows are recognised and never counted twice.
        """
        raw = self.data["resources"][resource_id].get("emitted", [])
        return {_parse_iso(v) for v in raw}

    def advance(
        self,
        resource_id: str,
        watermark: datetime,
        emitted: set[datetime],
        added_wh: int,
    ) -> None:
        entry = self.data["resources"][resource_id]
        entry["watermark"] = watermark.isoformat()
        entry["emitted"] = [dt.isoformat() for dt in sorted(emitted)]
        entry["cumulative_wh"] = int(entry["cumulative_wh"]) + int(added_wh)

    def cumulative_wh(self, resource_id: str) -> int:
        return int(self.data["resources"][resource_id]["cumulative_wh"])


def _migrate_state(data: dict[str, Any]) -> dict[str, Any]:
    """Bring an older on-disk state layout up to the current schema.

    Migration preserves cumulative totals so upgrading the bridge does not
    manufacture a spurious meter-reset in Home Assistant. Only forward
    migration is supported; an unrecognised (e.g. future) schema falls
    through to the caller's fresh-start handling.

    v1 -> v2: adds the per-resource ``emitted`` ledger (the set of
    already-emitted window starts ahead of the watermark). A v1 watermark
    was a strict contiguous frontier with nothing emitted above it, so the
    correct v2 value is an empty ledger.
    """
    if data.get("schema") == 1:
        for entry in data.get("resources", {}).values():
            entry.setdefault("emitted", [])
        data["schema"] = 2
    return data


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


def initial_watermark(now: datetime) -> datetime:
    """Seed watermark for a resource with no prior state.

    Fixed lookback rather than civil-day-start: avoids a timezone config
    knob and makes the first published cumulative value predictable
    regardless of what time of day the bridge is first started.
    """
    return floor_half_hour(now - INITIAL_BACKFILL)


def floor_half_hour(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    return dt.replace(minute=(dt.minute // 30) * 30, second=0, microsecond=0)


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
    start: datetime  # interval start, UTC, half-hour aligned
    kwh: float


@dataclasses.dataclass(frozen=True)
class Emission:
    new_watermark: datetime
    new_emitted: set[datetime]
    added_wh: int
    intervals: int


def select_emittable(
    readings: list[Reading],
    watermark: datetime,
    emitted: set[datetime],
    now: datetime,
    finalisation_lag: int,
    heal_horizon: int,
) -> Emission:
    """Choose which intervals to fold into the cumulative total.

    The state is a contiguous frontier (``watermark``) plus a *ledger*
    (``emitted``): the set of window starts strictly ahead of the frontier
    that have already been counted while a gap below them holds the
    frontier back. Together they give exactly-once, monotonic emission
    without a stall mode:

      * A window is finalised once its END is at or before
        ``now - finalisation_lag``; younger data may still be revised by
        the DCC and is left alone.
      * Any finalised window at or above the watermark that is not already
        in the ledger is emitted, *in whatever order it arrives*. A gap no
        longer halts emission — later windows are folded in immediately and
        recorded in the ledger. This is what makes a late-arriving interval
        self-heal exactly once: on the refetch that finally returns it, it
        is emitted and the ledger drains as the frontier sweeps over it.
      * The frontier then sweeps forward across every contiguous ledger
        entry (draining them), so ``data_complete_to`` still means "all
        windows up to here are accounted for".
      * A gap that outlives ``heal_horizon`` is abandoned: the frontier
        steps over it (accepting that one lost half-hour) so a permanently
        undelivered window cannot freeze the frontier forever. The ledger
        is thereby bounded to roughly ``heal_horizon`` of entries.

    Windows below the watermark are ignored: they were emitted and swept
    long ago, and the API is never refetched behind the frontier.
    """
    cutoff = now - timedelta(seconds=finalisation_lag)
    abandon_before = now - timedelta(seconds=heal_horizon)
    step = timedelta(seconds=HALF_HOUR)

    emitted = set(emitted)
    added_wh = 0
    count = 0
    for reading in readings:
        start = reading.start
        if start < watermark or start in emitted:
            continue  # already counted (below frontier or already in ledger)
        if start + step > cutoff:
            continue  # not yet finalised
        added_wh += round(reading.kwh * 1000)
        emitted.add(start)
        count += 1

    # Sweep the contiguous frontier forward, draining ledger entries as it
    # passes them and stepping over gaps too old to keep waiting on.
    cursor = watermark
    while True:
        if cursor in emitted:
            emitted.discard(cursor)
            cursor += step
            continue
        if cursor + step <= cutoff and cursor < abandon_before:
            log.warning(
                "gap at %s exceeded heal horizon; abandoning window",
                cursor.isoformat(),
            )
            cursor += step
            continue
        break

    return Emission(
        new_watermark=cursor,
        new_emitted=emitted,
        added_wh=added_wh,
        intervals=count,
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
            "period": "PT30M",
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
# MQTT publisher
# --------------------------------------------------------------------------


class MqttPublisher:
    """Write-only MQTT client. Subscribes to nothing, by design."""

    PUBLISH_TIMEOUT = 10
    CONNECT_TIMEOUT = 15

    def __init__(self, cfg: MqttConfig):
        import paho.mqtt.client as mqtt

        self._mqtt = mqtt
        self.cfg = cfg
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=cfg.client_id,
            protocol=mqtt.MQTTv311,
        )
        if cfg.username:
            self.client.username_pw_set(cfg.username, cfg.password or None)
        if cfg.tls.enabled:
            self.client.tls_set(
                ca_certs=cfg.tls.ca_cert or None,
                certfile=cfg.tls.client_cert or None,
                keyfile=cfg.tls.client_key or None,
            )
            if cfg.tls.insecure_skip_verify:
                self.client.tls_insecure_set(True)
        self.availability_topic = f"{cfg.topic_prefix}/bridge/availability"
        self.client.will_set(
            self.availability_topic, payload="offline", qos=cfg.qos, retain=True
        )

    def connect(self) -> None:
        self.client.connect(self.cfg.host, self.cfg.port, keepalive=60)
        self.client.loop_start()
        deadline = time.monotonic() + self.CONNECT_TIMEOUT
        while not self.client.is_connected():
            if time.monotonic() > deadline:
                raise CycleError(
                    f"MQTT connect to {self.cfg.host}:{self.cfg.port} timed out"
                )
            time.sleep(0.1)
        self.publish(self.availability_topic, "online", retain=True)

    def close(self) -> None:
        try:
            self.publish(self.availability_topic, "offline", retain=True)
        except Exception:
            pass
        self.client.loop_stop()
        self.client.disconnect()

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        info = self.client.publish(
            topic, payload, qos=self.cfg.qos, retain=retain
        )
        info.wait_for_publish(timeout=self.PUBLISH_TIMEOUT)
        if not info.is_published():
            raise CycleError(f"publish to {topic} not acknowledged")


# --------------------------------------------------------------------------
# Topics and Home Assistant discovery
# --------------------------------------------------------------------------


def state_topic(prefix: str, resource_id: str) -> str:
    return f"{prefix}/{resource_id}/state"


def status_topic(prefix: str, resource_id: str) -> str:
    return f"{prefix}/{resource_id}/status"


def bridge_status_topic(prefix: str) -> str:
    return f"{prefix}/bridge/status"


def discovery_payload(
    cfg: MqttConfig, resource_id: str, classifier: str, name: str
) -> tuple[str, str]:
    """Home Assistant MQTT discovery config for one consumption sensor.

    unique_id and the topic layout are treated as ABI: changing them
    orphans users' entities and breaks Energy dashboard configuration.
    """
    object_id = f"glowbridge_{resource_id.replace('-', '_')}"
    topic = f"{cfg.discovery_prefix}/sensor/{object_id}/config"
    payload = {
        "name": name,
        "unique_id": object_id,
        "state_topic": state_topic(cfg.topic_prefix, resource_id),
        "json_attributes_topic": status_topic(cfg.topic_prefix, resource_id),
        "availability_topic": f"{cfg.topic_prefix}/bridge/availability",
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit_of_measurement": "kWh",
        "suggested_display_precision": 3,
        "device": {
            "identifiers": ["glowbridge"],
            "name": "glowbridge",
            "manufacturer": "glowbridge",
            "sw_version": VERSION,
        },
    }
    return topic, json.dumps(payload, sort_keys=True)


# --------------------------------------------------------------------------
# Cycle runner
# --------------------------------------------------------------------------


class Bridge:
    def __init__(
        self,
        cfg: Config,
        state: StateFile,
        client: GlowmarktClient,
        publisher: MqttPublisher | None,
    ):
        self.cfg = cfg
        self.state = state
        self.client = client
        self.publisher = publisher  # None in --dry-run
        self.consecutive_failures = 0
        self.last_success: datetime | None = None

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
                        rid, "pinned", rid, initial_watermark(now)
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
                rid, classifier, name, initial_watermark(now)
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
        """One poll: fetch finalised intervals per resource, persist, publish.

        Ordering is load-bearing: state file first, then sensor state, then
        status. A crash mid-sequence must leave the broker conservative
        (status never claims more than was published) and the file ahead of
        the broker (a gap, never a double-count).
        """
        self.ensure_token(now)
        resources = self.resolve_resources(now)

        emissions: dict[str, Emission] = {}
        for rid in resources:
            try:
                emissions[rid] = self._fetch_resource(rid, now)
            except TokenExpired:
                log.info("token rejected; re-authenticating")
                self.state.clear_token()
                self.client.set_token("")
                self.ensure_token(now, force=True)
                emissions[rid] = self._fetch_resource(rid, now)
            except ResourceMissing:
                self.rediscover(rid, now)
                # Freshly discovered resources are picked up next cycle;
                # this cycle continues with the survivors.

        # Persist every fetched resource, not only those with new energy:
        # the frontier and ledger can move on their own when a gap is
        # abandoned past the heal horizon (intervals == 0, watermark still
        # advances), and that movement must survive a crash.
        for rid, e in emissions.items():
            self.state.advance(rid, e.new_watermark, e.new_emitted, e.added_wh)
        self.state.save()

        # The sensor state topic carries the cumulative total, so it only
        # needs republishing when new windows were folded in this cycle.
        changed = [rid for rid, e in emissions.items() if e.intervals > 0]

        self.last_success = now
        self.consecutive_failures = 0

        if self.publisher is None:
            self._print_dry_run(emissions)
            return

        for rid in changed:
            kwh = self.state.cumulative_wh(rid) / 1000
            self.publisher.publish(
                state_topic(self.cfg.mqtt.topic_prefix, rid), f"{kwh:.3f}"
            )
        for rid in emissions:
            self.publisher.publish(
                status_topic(self.cfg.mqtt.topic_prefix, rid),
                self._resource_status(rid, now),
                retain=True,
            )
        self.publisher.publish(
            bridge_status_topic(self.cfg.mqtt.topic_prefix),
            self._bridge_status(now),
            retain=True,
        )
        log.info(
            "cycle complete: %d resource(s), %d with new data",
            len(emissions),
            len(changed),
        )

    def _fetch_resource(self, rid: str, now: datetime) -> Emission:
        entry = self.state.resource(rid)
        watermark = floor_half_hour(self.state.watermark(rid))
        emitted = self.state.emitted(rid)
        self.client.catchup(rid)
        readings: list[Reading] = []
        # Refetch from the frontier every cycle: windows in the ledger above
        # it are recognised and skipped, so this is how a gap left behind
        # earlier gets a chance to arrive and heal.
        for start, end in fetch_windows(watermark, now):
            readings.extend(self.client.get_readings(rid, start, end))
        emission = select_emittable(
            readings,
            watermark,
            emitted,
            now,
            self.cfg.schedule.finalisation_lag,
            self.cfg.schedule.heal_horizon,
        )
        log.debug(
            "resource %s (%s): %d interval(s) emitted, frontier %s, ledger %d ahead",
            rid,
            entry.get("classifier", "?") if entry else "?",
            emission.intervals,
            emission.new_watermark.isoformat(),
            len(emission.new_emitted),
        )
        return emission

    def publish_failure_status(self, now: datetime, next_planned: datetime) -> None:
        """After a failed cycle the bridge still reports in: status topics
        carry the pushed-out next_planned_update and the failure count so a
        limping bridge is visible, distinct from a dead one (LWT) and from
        a stale DCC feed (data_complete_to vs last_success)."""
        if self.publisher is None:
            return
        try:
            for rid in self.state.resource_ids():
                self.publisher.publish(
                    status_topic(self.cfg.mqtt.topic_prefix, rid),
                    self._resource_status(rid, now, next_planned),
                    retain=True,
                )
            self.publisher.publish(
                bridge_status_topic(self.cfg.mqtt.topic_prefix),
                self._bridge_status(now, next_planned),
                retain=True,
            )
        except Exception as exc:
            log.warning("could not publish failure status: %s", exc)

    def next_planned(self, now: datetime) -> datetime:
        ts = next_cycle_start(
            now.timestamp(),
            self.cfg.schedule.interval,
            install_offset(machine_seed(), self.cfg.schedule.jitter),
        )
        return datetime.fromtimestamp(ts, tz=UTC)

    def _status_base(
        self, now: datetime, next_planned: datetime | None
    ) -> dict[str, Any]:
        """Fields common to the resource and bridge status payloads."""
        return {
            "schema": STATUS_SCHEMA,
            "last_success": _iso_or_empty(self.last_success),
            "next_planned_update": (
                next_planned or self.next_planned(now)
            ).isoformat(),
            "consecutive_failures": self.consecutive_failures,
            "bridge_version": VERSION,
        }

    def _resource_status(
        self, rid: str, now: datetime, next_planned: datetime | None = None
    ) -> str:
        entry = self.state.resource(rid) or {}
        payload = self._status_base(now, next_planned)
        payload["data_complete_to"] = entry.get("watermark", "")
        payload["cumulative_kwh"] = round(
            int(entry.get("cumulative_wh", 0)) / 1000, 3
        )
        return json.dumps(payload, sort_keys=True)

    def _bridge_status(
        self, now: datetime, next_planned: datetime | None = None
    ) -> str:
        payload = self._status_base(now, next_planned)
        payload["resources"] = self.state.resource_ids()
        return json.dumps(payload, sort_keys=True)

    def _print_dry_run(self, emissions: dict[str, Emission]) -> None:
        for rid, emission in emissions.items():
            kwh = self.state.cumulative_wh(rid) / 1000
            print(
                f"[dry-run] {state_topic(self.cfg.mqtt.topic_prefix, rid)}"
                f" -> {kwh:.3f} ({emission.intervals} new interval(s),"
                f" watermark {emission.new_watermark.isoformat()})"
            )

    def publish_discovery(self) -> None:
        if self.publisher is None:
            return
        for rid in self.state.resource_ids():
            entry = self.state.resource(rid) or {}
            topic, payload = discovery_payload(
                self.cfg.mqtt,
                rid,
                entry.get("classifier", ""),
                entry.get("name", rid),
            )
            self.publisher.publish(topic, payload, retain=True)


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
            break
        except RateLimited as exc:
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


class RedactingFormatter(logging.Formatter):
    """Formats then scrubs. Known secret values are replaced wholesale and
    token-ish JSON fields are masked, so a debug dump of a raw API response
    cannot leak credentials into a pasted log."""

    PATTERNS = [
        re.compile(r'("(?:token|password)"\s*:\s*")[^"]*(")', re.IGNORECASE),
    ]

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
        for secret in self.secrets:
            rendered = rendered.replace(secret, "***")
        for pattern in self.PATTERNS:
            rendered = pattern.sub(r"\1***\2", rendered)
        return rendered


def setup_logging(cfg: Config, debug_override: bool) -> None:
    level = "debug" if debug_override else cfg.logging.level
    handler = logging.StreamHandler(sys.stderr)
    secrets = [
        cfg.glowmarkt.password,
        cfg.mqtt.password,
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
    # paho and urllib3 are chatty at DEBUG and their payload logging is not
    # redacted by this formatter's secret list alone; keep them at INFO.
    logging.getLogger("paho").setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.INFO)


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
        description="Bridge Glowmarkt/DCC smart meter readings to MQTT.",
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
        help="fetch and print what would be published; no MQTT, no state writes",
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

    publisher: MqttPublisher | None = None
    if not args.dry_run:
        publisher = MqttPublisher(cfg.mqtt)
        try:
            publisher.connect()
        except Exception as exc:
            log.error("cannot connect to MQTT broker: %s", exc)
            return 1

    bridge = Bridge(cfg, state, client, publisher)

    if args.dry_run:
        # Dry runs must not mutate persistent state.
        state.save = lambda: None  # type: ignore[method-assign]

    try:
        if args.once or args.dry_run:
            ok = run_cycle_with_retries(bridge, cfg)
            if ok and publisher is not None:
                bridge.publish_discovery()
            return 0 if ok else 1
        return _daemon_loop(bridge, cfg, publisher)
    finally:
        if publisher is not None:
            publisher.close()


def _daemon_loop(
    bridge: Bridge, cfg: Config, publisher: MqttPublisher | None
) -> int:
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

    first = True
    while not shutdown.requested:
        ok = run_cycle_with_retries(bridge, cfg)
        now = datetime.now(tz=UTC)
        if ok and first and publisher is not None:
            # Discovery needs the resource list, which may only exist after
            # the first successful cycle.
            bridge.publish_discovery()
            first = False
        next_ts = next_cycle_start(time.time(), cfg.schedule.interval, offset)
        if not ok:
            bridge.publish_failure_status(
                now, datetime.fromtimestamp(next_ts, tz=UTC)
            )
        log.info(
            "next cycle at %s",
            datetime.fromtimestamp(next_ts, tz=UTC).isoformat(),
        )
        shutdown.sleep_until(next_ts)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
