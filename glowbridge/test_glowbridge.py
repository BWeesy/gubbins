#!/usr/bin/env -S uv run --script
# SPDX-License-Identifier: MIT
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest>=8",
#     "requests>=2.31",
#     "websocket-client>=1.7",
# ]
# ///
"""Tests for glowbridge.

Coverage is concentrated where the subtle bugs live: the hourly cumulative
walk (sum-through-end-of-hour, gaps, finalisation, frontier advance),
revision re-import, conditional catchup, the WebSocket import handshake,
state migration/fresh-start, redaction, and crash ordering. The Glowmarkt
API and the Home Assistant WebSocket are exercised through fakes; there are
deliberately no live-API/HA tests.

Run: uv run test_glowbridge.py   (or: pytest test_glowbridge.py)
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import glowbridge as gb

HOUR = timedelta(seconds=gb.HOUR)


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------

MINIMAL_CONFIG = """
[glowmarkt]
username = "user@example.com"
password = "hunter2"

[homeassistant]
url = "ws://ha.local:8123/api/websocket"
"""


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "glowbridge.toml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def load_cfg(tmp_path: Path, text: str, env: dict | None = None) -> gb.Config:
    return gb.load_config(write_config(tmp_path, text), environ=env or {})


def make_readings(start: datetime, values: list[float | None]) -> list[gb.Reading]:
    """Hourly readings from `start`; a None value is a gap (no reading)."""
    out = []
    for i, value in enumerate(values):
        if value is None:
            continue
        out.append(gb.Reading(start=start + i * HOUR, kwh=value))
    return out


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class TestConfig:
    def test_minimal_config_loads_with_defaults(self, tmp_path):
        cfg = load_cfg(tmp_path, MINIMAL_CONFIG)
        assert cfg.glowmarkt.username == "user@example.com"
        assert cfg.homeassistant.url == "ws://ha.local:8123/api/websocket"
        assert cfg.schedule.interval == 1800
        assert cfg.schedule.finalisation_lag == 5400
        assert cfg.schedule.backfill_lookback == 31536000
        assert cfg.schedule.revision_window == 604800
        assert cfg.schedule.catchup_stale_after == 86400
        assert cfg.retry.auth_floor == 3600

    def test_unknown_key_fails_with_path(self, tmp_path):
        text = MINIMAL_CONFIG + "\n[schedule]\njiter = 300\n"
        with pytest.raises(gb.ConfigError, match="schedule.jiter"):
            load_cfg(tmp_path, text)

    def test_unknown_section_fails(self, tmp_path):
        text = MINIMAL_CONFIG + "\n[mqtt]\nhost = \"x\"\n"
        with pytest.raises(gb.ConfigError, match="mqtt"):
            load_cfg(tmp_path, text)

    def test_wrong_type_fails(self, tmp_path):
        text = MINIMAL_CONFIG + "\n[schedule]\ninterval = \"30m\"\n"
        with pytest.raises(gb.ConfigError, match="schedule.interval"):
            load_cfg(tmp_path, text)

    def test_bool_is_not_int(self, tmp_path):
        text = MINIMAL_CONFIG + "\n[schedule]\ninterval = true\n"
        with pytest.raises(gb.ConfigError, match="boolean"):
            load_cfg(tmp_path, text)

    def test_missing_credentials_fails(self, tmp_path):
        text = "[homeassistant]\nurl = \"ws://ha/api/websocket\"\n"
        with pytest.raises(gb.ConfigError, match="glowmarkt.username"):
            load_cfg(tmp_path, text)

    def test_missing_ha_url_fails(self, tmp_path):
        text = "[glowmarkt]\nusername = \"u\"\npassword = \"p\"\n"
        with pytest.raises(gb.ConfigError, match="homeassistant.url"):
            load_cfg(tmp_path, text)

    def test_bad_ha_url_scheme_fails(self, tmp_path):
        text = MINIMAL_CONFIG.replace("ws://ha.local:8123/api/websocket", "http://ha/x")
        with pytest.raises(gb.ConfigError, match="ws:// or wss://"):
            load_cfg(tmp_path, text)

    def test_interval_minimum_enforced(self, tmp_path):
        text = MINIMAL_CONFIG + "\n[schedule]\ninterval = 600\n"
        with pytest.raises(gb.ConfigError, match="1800"):
            load_cfg(tmp_path, text)

    def test_revision_window_must_cover_finalisation_lag(self, tmp_path):
        text = MINIMAL_CONFIG + (
            "\n[schedule]\nfinalisation_lag = 5400\nrevision_window = 3600\n"
        )
        with pytest.raises(gb.ConfigError, match="revision_window"):
            load_cfg(tmp_path, text)

    def test_env_overrides_secrets(self, tmp_path):
        env = {
            gb.ENV_GLOW_USERNAME: "env@example.com",
            gb.ENV_GLOW_PASSWORD: "env-pass",
            gb.ENV_HA_TOKEN: "env-token",
        }
        cfg = load_cfg(tmp_path, MINIMAL_CONFIG, env)
        assert cfg.glowmarkt.username == "env@example.com"
        assert cfg.glowmarkt.password == "env-pass"
        assert cfg.homeassistant.token == "env-token"

    def test_ha_token_from_file(self, tmp_path):
        text = MINIMAL_CONFIG + "\ntoken = \"file-token\"\n"
        cfg = load_cfg(tmp_path, text)
        assert cfg.homeassistant.token == "file-token"

    def test_secretful_readable_config_warns(self, tmp_path, caplog):
        path = write_config(tmp_path, MINIMAL_CONFIG)
        path.chmod(0o644)
        with caplog.at_level("WARNING"):
            gb.load_config(path, environ={})
        assert any("chmod 600" in r.message for r in caplog.records)

    def test_secretless_readable_config_does_not_warn(self, tmp_path, caplog):
        text = "[homeassistant]\nurl = \"ws://ha/api/websocket\"\n"
        env = {gb.ENV_GLOW_USERNAME: "u", gb.ENV_GLOW_PASSWORD: "p"}
        path = write_config(tmp_path, text)
        path.chmod(0o644)
        with caplog.at_level("WARNING"):
            gb.load_config(path, environ=env)
        assert not any("chmod 600" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------


class TestScheduling:
    def test_offset_deterministic_and_bounded(self):
        a = gb.install_offset("machine-a", 300)
        assert a == gb.install_offset("machine-a", 300)  # deterministic
        assert 0 <= a <= 300  # bounded to the jitter window

    def test_offset_zero_jitter(self):
        assert gb.install_offset("anything", 0) == 0

    def test_next_cycle_is_in_future_and_anchored(self):
        now = 1_000_000.0
        nxt = gb.next_cycle_start(now, 1800, 0)
        assert nxt > now
        assert nxt % 1800 == 0

    def test_next_cycle_on_boundary_moves_forward(self):
        now = float(1800 * 5)
        nxt = gb.next_cycle_start(now, 1800, 0)
        assert nxt == 1800 * 6

    def test_initial_settled_is_lookback_hour_aligned(self):
        now = datetime(2026, 7, 20, 14, 37, 12, tzinfo=UTC)
        seeded = gb.initial_settled(now, 3 * 3600)
        assert seeded == datetime(2026, 7, 20, 11, 0, tzinfo=UTC)

    def test_floor_hour(self):
        dt = datetime(2026, 7, 20, 14, 37, 45, tzinfo=UTC)
        assert gb.floor_hour(dt) == datetime(2026, 7, 20, 14, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# plan_import — the emission core
# --------------------------------------------------------------------------


class TestPlanImport:
    WM = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    def plan(self, readings, settled, cum, now, lag=0, rev=0):
        return gb.plan_import(readings, settled, cum, now, lag, rev)

    def test_contiguous_hours_cumulative_sums(self):
        readings = make_readings(self.WM, [0.5, 0.25, 0.25])
        now = self.WM + 3 * HOUR
        p = self.plan(readings, self.WM, 0, now)
        # sum at each hour is the running total THROUGH that hour.
        assert p.rows == [
            {"start": self.WM.isoformat(), "sum": 0.5},
            {"start": (self.WM + HOUR).isoformat(), "sum": 0.75},
            {"start": (self.WM + 2 * HOUR).isoformat(), "sum": 1.0},
        ]
        assert p.imported_hours == 3
        assert p.data_complete_to == self.WM + 2 * HOUR
        # rev=0, now hour-aligned: frontier settles at now, baseline = total.
        assert p.new_settled_through == now
        assert p.new_cumulative_wh == 1000

    def test_cumulative_builds_on_baseline(self):
        readings = make_readings(self.WM, [0.5])
        now = self.WM + HOUR
        p = self.plan(readings, self.WM, 2000, now)  # baseline 2000 Wh
        assert p.rows == [{"start": self.WM.isoformat(), "sum": 2.5}]
        assert p.new_cumulative_wh == 2500

    def test_gap_hour_skipped_not_zero_filled(self):
        # hour WM+1 missing; later hours still import, cumulative excludes gap.
        readings = make_readings(self.WM, [0.5, None, 0.3])
        now = self.WM + 3 * HOUR
        p = self.plan(readings, self.WM, 0, now)
        starts = [r["start"] for r in p.rows]
        assert (self.WM + HOUR).isoformat() not in starts  # gap: no row
        assert p.rows[-1] == {"start": (self.WM + 2 * HOUR).isoformat(), "sum": 0.8}
        # WM+2 consumption in HA = 0.8 - 0.5 = 0.3, the gap not misattributed.

    def test_finalisation_lag_holds_back_recent_hours(self):
        readings = make_readings(self.WM, [0.5, 0.25])
        now = self.WM + 2 * HOUR
        # lag one hour: only the first hour is finalised.
        p = self.plan(readings, self.WM, 0, now, lag=3600, rev=3600)
        assert p.imported_hours == 1
        assert p.rows[0]["start"] == self.WM.isoformat()

    def test_finalisation_lag_zero_imports_up_to_now(self):
        readings = make_readings(self.WM, [0.5, 0.25, 0.25, 0.5])
        now = self.WM + 4 * HOUR
        p = self.plan(readings, self.WM, 0, now, lag=0)
        assert p.imported_hours == 4  # every hour up to now

    def test_frontier_holds_back_revision_window(self):
        readings = make_readings(self.WM, [0.5, 0.25, 0.25, 0.5])
        now = self.WM + 4 * HOUR
        # keep the last hour re-checkable.
        p = self.plan(readings, self.WM, 0, now, lag=0, rev=3600)
        assert p.new_settled_through == self.WM + 3 * HOUR
        # cumulative baseline is the total THROUGH the frontier (3 hours).
        assert p.new_cumulative_wh == 1000
        assert p.imported_hours == 4  # all four still imported this cycle

    def test_no_readings_advances_frontier_without_rows(self):
        now = self.WM + 4 * HOUR
        p = self.plan([], self.WM, 500, now, lag=0, rev=3600)
        assert p.rows == []
        assert p.data_complete_to is None
        assert p.new_settled_through == self.WM + 3 * HOUR
        assert p.new_cumulative_wh == 500  # unchanged: nothing to add

    def test_late_fill_reimports_slice_across_cycles(self):
        # Cycle 1: WM+1 missing; frontier kept behind it by the revision window.
        now1 = self.WM + 3 * HOUR
        p1 = self.plan(make_readings(self.WM, [0.5, None, 0.3]),
                       self.WM, 0, now1, lag=0, rev=2 * 3600)
        assert p1.new_settled_through == self.WM + HOUR  # now1 - 2h
        assert p1.new_cumulative_wh == 500  # only WM counted before the frontier

        # Cycle 2: the DCC delivers WM+1; re-fetch from the frontier re-imports
        # it and everything after, with shifted sums — exactly-once, no ledger.
        now2 = self.WM + 3 * HOUR
        p2 = self.plan(make_readings(self.WM, [0.5, 0.4, 0.3]),
                       p1.new_settled_through, p1.new_cumulative_wh, now2,
                       lag=0, rev=0)
        starts = {r["start"]: r["sum"] for r in p2.rows}
        assert starts[(self.WM + HOUR).isoformat()] == 0.9   # 0.5 + 0.4
        assert starts[(self.WM + 2 * HOUR).isoformat()] == 1.2  # + 0.3


# --------------------------------------------------------------------------
# fetch_windows
# --------------------------------------------------------------------------


class TestFetchWindows:
    def test_single_window_for_short_span(self):
        start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
        end = start + timedelta(hours=6)
        assert gb.fetch_windows(start, end) == [(start, end)]

    def test_long_span_chunked_below_31_days(self):
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        end = start + timedelta(days=95)
        windows = gb.fetch_windows(start, end)
        assert windows[0][0] == start
        assert windows[-1][1] == end
        for a, b in windows:
            assert b - a <= gb.MAX_FETCH_SPAN
            assert gb.MAX_FETCH_SPAN <= timedelta(days=31)

    def test_empty_span(self):
        start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
        assert gb.fetch_windows(start, start) == []


# --------------------------------------------------------------------------
# State file (schema 3)
# --------------------------------------------------------------------------


class TestStateFile:
    def test_round_trip(self, tmp_path):
        state = gb.StateFile(tmp_path)
        wm = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
        state.upsert_resource("res-1", "electricity.consumption", "Elec", wm)
        state.mark_catchup("res-1", wm + HOUR)
        state.commit("res-1", wm + 2 * HOUR, 1500, wm + 3 * HOUR)
        state.set_token("secret-token", datetime.now(tz=UTC))
        state.save()

        reloaded = gb.StateFile(tmp_path)
        reloaded.load()
        assert reloaded.settled_through("res-1") == wm + 2 * HOUR
        assert reloaded.cumulative_at_settled("res-1") == 1500
        assert reloaded.data_complete_to("res-1") == wm + 3 * HOUR
        assert reloaded.last_catchup_at("res-1") == wm + HOUR
        assert reloaded.token == "secret-token"

    def test_old_schema_starts_fresh(self, tmp_path):
        # A schema-2 (MQTT-era) file is not migrated: treated as fresh, which
        # triggers a backfill on next run.
        old = {
            "schema": 2,
            "auth": {"token": "t", "obtained_at": "", "last_attempt_at": ""},
            "resources": {"res-1": {"watermark": "2026-01-01T00:00:00+00:00"}},
        }
        state = gb.StateFile(tmp_path)
        state.path.parent.mkdir(parents=True, exist_ok=True)
        state.path.write_text(json.dumps(old), encoding="utf-8")
        state.load()
        assert state.data["schema"] == gb.STATE_SCHEMA
        assert state.data["resources"] == {}
        assert state.token == ""

    def test_corrupt_state_starts_fresh(self, tmp_path, caplog):
        state = gb.StateFile(tmp_path)
        state.path.parent.mkdir(parents=True, exist_ok=True)
        state.path.write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            state.load()
        assert state.data["resources"] == {}
        assert any("corrupt" in r.message for r in caplog.records)

    def test_missing_file_is_fresh(self, tmp_path):
        state = gb.StateFile(tmp_path / "nowhere")
        state.load()
        assert state.data["resources"] == {}

    def test_atomic_write_leaves_no_tmp(self, tmp_path):
        state = gb.StateFile(tmp_path)
        state.save()
        assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


class TestRedaction:
    def test_redact_scrubs_secret_values(self):
        out = gb.redact("password is hunter2 ok", ["hunter2"])
        assert "hunter2" not in out
        assert "***" in out

    def test_redact_masks_json_credential_fields(self):
        out = gb.redact('{"access_token": "eyJabc.def", "type": "auth"}', [])
        assert "eyJabc" not in out
        assert '"access_token": "***"' in out

    def test_redact_masks_token_and_password_fields(self):
        out = gb.redact('{"token": "abc", "password": "def"}', [])
        assert "abc" not in out and "def" not in out

    def test_formatter_json_mode_redacts(self):
        fmt = gb.RedactingFormatter("%(message)s", secrets=["s3cret"], json_mode=True)
        record = logging.LogRecord(
            "glowbridge", logging.INFO, __file__, 1, "value s3cret here", None, None
        )
        out = fmt.format(record)
        assert "s3cret" not in out
        json.loads(out)  # still valid JSON


# --------------------------------------------------------------------------
# statistic_id
# --------------------------------------------------------------------------


class TestStatisticId:
    def test_classifier_based_for_known_consumption(self):
        assert (
            gb.statistic_id_for("abc-123", "electricity.consumption")
            == "glowbridge:electricity_consumption"
        )
        assert (
            gb.statistic_id_for("abc-123", "gas.consumption")
            == "glowbridge:gas_consumption"
        )

    def test_resource_id_fallback_for_pinned(self):
        assert (
            gb.statistic_id_for("abc-123", "pinned") == "glowbridge:abc_123"
        )


# --------------------------------------------------------------------------
# HA WebSocket importer (fake socket)
# --------------------------------------------------------------------------


class FakeWs:
    def __init__(self, script):
        self.script = list(script)  # dicts returned from recv() in order
        self.sent: list[dict] = []
        self.closed = False

    def settimeout(self, t):
        pass

    def send(self, data):
        self.sent.append(json.loads(data))

    def recv(self):
        return json.dumps(self.script.pop(0))

    def close(self):
        self.closed = True


class FakeWsModule:
    def __init__(self, ws):
        self._ws = ws
        self.urls: list[str] = []

    def create_connection(self, url, timeout=None):
        self.urls.append(url)
        return self._ws


def make_importer(script, token="tok"):
    imp = gb.HaImporter(gb.HomeAssistantConfig(url="ws://ha/api/websocket", token=token))
    ws = FakeWs(script)
    imp._websocket = FakeWsModule(ws)
    return imp, ws


class TestHaImporter:
    OK = {"type": "auth_ok"}
    REQ = {"type": "auth_required"}

    def test_connect_and_import(self):
        imp, ws = make_importer([self.REQ, self.OK, {"id": 1, "success": True}])
        imp.connect()
        imp.import_statistics(
            "glowbridge:electricity_consumption",
            "Electricity consumption",
            [{"start": "2026-07-20T00:00:00+00:00", "sum": 0.5}],
        )
        # auth message then import message.
        assert ws.sent[0] == {"type": "auth", "access_token": "tok"}
        msg = ws.sent[1]
        assert msg["type"] == "recorder/import_statistics"
        assert msg["metadata"]["statistic_id"] == "glowbridge:electricity_consumption"
        assert msg["metadata"]["source"] == "glowbridge"
        assert msg["metadata"]["has_sum"] is True
        assert msg["metadata"]["unit_of_measurement"] == "kWh"
        assert msg["stats"][0]["sum"] == 0.5

    def test_auth_invalid_raises_autherror(self):
        imp, _ = make_importer([self.REQ, {"type": "auth_invalid"}])
        with pytest.raises(gb.AuthError):
            imp.connect()

    def test_missing_token_raises_autherror(self):
        imp, _ = make_importer([self.REQ, self.OK], token="")
        with pytest.raises(gb.AuthError, match="token not set"):
            imp.connect()

    def test_import_failure_raises_cycleerror(self):
        imp, _ = make_importer(
            [self.REQ, self.OK, {"id": 1, "success": False, "error": {"message": "nope"}}]
        )
        imp.connect()
        with pytest.raises(gb.CycleError, match="rejected import"):
            imp.import_statistics("glowbridge:x", "X", [])


# --------------------------------------------------------------------------
# Fakes for cycle wiring
# --------------------------------------------------------------------------


class FakeGlowClient:
    def __init__(self, readings_by_resource=None, resources=None):
        self.readings_by_resource = readings_by_resource or {}
        self.resources = resources or []
        self.token = ""
        self.auth_calls = 0
        self.fail_auth = False
        self.fail_readings: list[Exception] = []
        self.catchups: list[str] = []

    def set_token(self, token):
        self.token = token

    def authenticate(self):
        self.auth_calls += 1
        if self.fail_auth:
            raise gb.AuthError("authentication rejected (HTTP 401)")
        self.token = f"token-{self.auth_calls}"
        return self.token

    def get_resources(self):
        return self.resources

    def catchup(self, resource_id):
        self.catchups.append(resource_id)

    def get_readings(self, resource_id, start, end):
        if self.fail_readings:
            raise self.fail_readings.pop(0)
        return [
            r
            for r in self.readings_by_resource.get(resource_id, [])
            if start <= r.start < end
        ]


class FakeImporter:
    def __init__(self):
        self.ws = None
        self.imports: list[tuple] = []
        self.connects = 0
        self.closes = 0
        self.fail_connect: Exception | None = None

    def connect(self):
        self.connects += 1
        if self.fail_connect:
            raise self.fail_connect
        self.ws = object()

    def import_statistics(self, statistic_id, name, stats):
        self.imports.append((statistic_id, name, list(stats)))

    def close(self):
        self.closes += 1
        self.ws = None


def make_bridge(tmp_path, client, importer, **schedule):
    cfg = gb.Config(
        glowmarkt=gb.GlowmarktConfig(username="u", password="p"),
        homeassistant=gb.HomeAssistantConfig(
            url="ws://ha/api/websocket", token="t"
        ),
        schedule=gb.ScheduleConfig(
            finalisation_lag=0,
            revision_window=3600,
            backfill_lookback=4 * 3600,
            **schedule,
        ),
        retry=gb.RetryConfig(auth_floor=0),
    )
    state = gb.StateFile(tmp_path)
    state.load()
    return gb.Bridge(cfg, state, client, importer), state


ELEC = {
    "resourceId": "res-elec",
    "classifier": "electricity.consumption",
    "name": "electricity consumption",
}
COST = {
    "resourceId": "res-cost",
    "classifier": "electricity.consumption.cost",
    "name": "electricity cost",
}


class TestCycle:
    WM = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)  # hour-aligned

    def _client(self, values):
        return FakeGlowClient(
            readings_by_resource={"res-elec": make_readings(self.WM, values)},
            resources=[ELEC, COST],
        )

    def test_first_cycle_discovers_imports_commits(self, tmp_path):
        client = self._client([0.5, 0.25, 0.25, 0.5])
        importer = FakeImporter()
        bridge, state = make_bridge(tmp_path, client, importer)
        now = self.WM + 4 * HOUR

        bridge.run_cycle(now)

        assert state.resource_ids() == ["res-elec"]  # cost filtered out
        assert importer.connects == 1 and importer.closes == 1
        assert len(importer.imports) == 1
        sid, name, rows = importer.imports[0]
        assert sid == "glowbridge:electricity_consumption"
        assert rows[0] == {"start": self.WM.isoformat(), "sum": 0.5}
        assert rows[-1] == {"start": (self.WM + 3 * HOUR).isoformat(), "sum": 1.5}
        # frontier held one hour back (revision_window=3600).
        assert state.settled_through("res-elec") == self.WM + 3 * HOUR
        assert state.cumulative_at_settled("res-elec") == 1000
        assert state.data_complete_to("res-elec") == self.WM + 3 * HOUR

    def test_status_file_written(self, tmp_path):
        client = self._client([0.5, 0.25])
        bridge, state = make_bridge(tmp_path, client, FakeImporter())
        bridge.run_cycle(self.WM + 4 * HOUR)
        status = json.loads(bridge.status_path.read_text(encoding="utf-8"))
        assert status["consecutive_failures"] == 0
        assert status["last_success"]
        assert status["last_error"] == ""
        assert "glowbridge:electricity_consumption" in status["resources"]

    def test_import_failure_does_not_advance_frontier(self, tmp_path):
        client = self._client([0.5, 0.25, 0.25, 0.5])
        importer = FakeImporter()
        importer.fail_connect = gb.CycleError("HA down")
        bridge, state = make_bridge(tmp_path, client, importer)
        with pytest.raises(gb.CycleError):
            bridge.run_cycle(self.WM + 4 * HOUR)
        # Import-then-commit: a failed import must leave the frontier unmoved
        # so next cycle retries the same window (idempotent).
        assert state.resource("res-elec")["settled_through"] == self.WM.isoformat()
        assert importer.closes == 1  # still closed in finally

    def test_stale_resource_triggers_throttled_catchup(self, tmp_path):
        # No readings, so data_complete_to stays stale across both cycles and
        # the 30-min throttle (not the staleness check) is what suppresses the
        # second nudge.
        client = FakeGlowClient(resources=[ELEC])
        bridge, state = make_bridge(tmp_path, client, FakeImporter())
        state.upsert_resource(
            "res-elec", "electricity.consumption", "Elec", self.WM
        )
        stale = self.WM - 2 * 24 * HOUR
        state.commit("res-elec", self.WM, 0, stale)
        now = self.WM + 4 * HOUR

        bridge.run_cycle(now)
        assert client.catchups == ["res-elec"]  # stale by > a day → nudged

        client.catchups.clear()
        bridge.run_cycle(now + timedelta(minutes=5))
        assert client.catchups == []  # within 30-min throttle → no 2nd nudge

    def test_fresh_resource_not_caught_up(self, tmp_path):
        client = self._client([0.5, 0.25, 0.25, 0.5])
        bridge, _ = make_bridge(tmp_path, client, FakeImporter())
        bridge.run_cycle(self.WM + 4 * HOUR)
        assert client.catchups == []  # nothing to nudge on a fresh backfill

    def test_token_expiry_reauths_once_and_continues(self, tmp_path):
        client = self._client([0.5, 0.25, 0.25, 0.5])
        client.fail_readings = [gb.TokenExpired()]
        bridge, state = make_bridge(tmp_path, client, FakeImporter())
        state.set_token("stale", datetime.now(tz=UTC))
        bridge.run_cycle(self.WM + 4 * HOUR)
        assert client.auth_calls == 1
        assert state.cumulative_at_settled("res-elec") == 1000

    def test_pinned_resource_404_is_fatal(self, tmp_path):
        client = FakeGlowClient()
        client.fail_readings = [gb.ResourceMissing("res-pin")]
        bridge, state = make_bridge(tmp_path, client, FakeImporter())
        object.__setattr__(
            bridge.cfg, "glowmarkt",
            gb.GlowmarktConfig(username="u", password="p", resources=("res-pin",)),
        )
        with pytest.raises(gb.CycleError, match="pinned resource"):
            bridge.run_cycle(self.WM + 4 * HOUR)

    def test_cached_resource_404_triggers_rediscovery(self, tmp_path):
        client = self._client([0.5, 0.25, 0.25, 0.5])
        bridge, state = make_bridge(tmp_path, client, FakeImporter())
        state.upsert_resource("res-dead", "electricity.consumption", "old", self.WM)
        client.fail_readings = [gb.ResourceMissing("res-dead")]
        bridge.run_cycle(self.WM + 4 * HOUR)
        assert state.resource("res-dead") is None
        assert "res-elec" in state.resource_ids()

    def test_auth_floor_blocks_reauth(self, tmp_path):
        client = FakeGlowClient(resources=[ELEC])
        bridge, state = make_bridge(tmp_path, client, FakeImporter())
        object.__setattr__(
            bridge.cfg, "retry", gb.RetryConfig(auth_floor=3600)
        )
        now = datetime.now(tz=UTC)
        state.mark_auth_attempt(now - timedelta(seconds=60))
        with pytest.raises(gb.AuthError, match="floor not met"):
            bridge.ensure_token(now)
        assert client.auth_calls == 0

    def test_dry_run_imports_nothing_and_writes_no_state(self, tmp_path, capsys):
        client = self._client([0.5, 0.25])
        bridge, state = make_bridge(tmp_path, client, None)  # importer None = dry-run
        state.save = lambda: None
        bridge.run_cycle(self.WM + 4 * HOUR)
        out = capsys.readouterr().out
        assert "[dry-run]" in out
        assert not (tmp_path / "state.json").exists()


# --------------------------------------------------------------------------
# Retry orchestration
# --------------------------------------------------------------------------


class TestRetryOrchestration:
    def _bridge_failing(self, tmp_path, times):
        client = FakeGlowClient(resources=[ELEC])
        client.fail_readings = [gb.CycleError(f"boom {i}") for i in range(times)]
        bridge, _ = make_bridge(tmp_path, client, FakeImporter())
        return bridge

    def test_retries_then_succeeds(self, tmp_path):
        client = FakeGlowClient(
            readings_by_resource={
                "res-elec": make_readings(TestCycle.WM, [0.5, 0.25, 0.25, 0.5])
            },
            resources=[ELEC],
        )
        client.fail_readings = [gb.CycleError("boom")]
        bridge, _ = make_bridge(tmp_path, client, FakeImporter())
        sleeps = []
        ok = gb.run_cycle_with_retries(bridge, bridge.cfg, sleeper=sleeps.append)
        assert ok
        assert len(sleeps) == 1
        assert bridge.consecutive_failures == 0

    def test_budget_exhaustion_records_error(self, tmp_path):
        bridge = self._bridge_failing(tmp_path, times=99)
        ok = gb.run_cycle_with_retries(bridge, bridge.cfg, sleeper=lambda s: None)
        assert not ok
        assert bridge.consecutive_failures == 1
        assert bridge.last_error

    def test_auth_error_short_circuits(self, tmp_path):
        client = FakeGlowClient(resources=[ELEC])
        client.fail_auth = True
        bridge, _ = make_bridge(tmp_path, client, FakeImporter())
        sleeps = []
        ok = gb.run_cycle_with_retries(bridge, bridge.cfg, sleeper=sleeps.append)
        assert not ok
        assert sleeps == []
        assert client.auth_calls == 1

    def test_rate_limit_respects_retry_after(self, tmp_path):
        client = FakeGlowClient(
            readings_by_resource={
                "res-elec": make_readings(TestCycle.WM, [0.5, 0.25, 0.25, 0.5])
            },
            resources=[ELEC],
        )
        client.fail_readings = [gb.RateLimited(retry_after=120)]
        bridge, _ = make_bridge(tmp_path, client, FakeImporter())
        sleeps = []
        ok = gb.run_cycle_with_retries(bridge, bridge.cfg, sleeper=sleeps.append)
        assert ok
        assert sleeps == [120]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
