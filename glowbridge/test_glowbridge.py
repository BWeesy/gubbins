#!/usr/bin/env -S uv run --script
# SPDX-License-Identifier: MIT
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest>=8",
#     "requests>=2.31",
#     "paho-mqtt>=2.0",
# ]
# ///
"""Tests for glowbridge.

The emission and time logic is where the subtle bugs live (DST transition
days, gap handling, monotonicity), so that is where the coverage is
concentrated. Network and broker behaviour is exercised through fakes; there
are deliberately no live-API tests here.

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

HH = timedelta(seconds=gb.HALF_HOUR)


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------

MINIMAL_CONFIG = """
[glowmarkt]
username = "user@example.com"
password = "hunter2"

[mqtt]
host = "broker.local"
"""


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "glowbridge.toml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def load_cfg(tmp_path: Path, text: str, env: dict | None = None) -> gb.Config:
    return gb.load_config(write_config(tmp_path, text), environ=env or {})


class TestConfig:
    def test_minimal_config_loads_with_defaults(self, tmp_path):
        cfg = load_cfg(tmp_path, MINIMAL_CONFIG)
        assert cfg.glowmarkt.username == "user@example.com"
        assert cfg.schedule.interval == 1800
        assert cfg.schedule.finalisation_lag == 5400
        assert cfg.schedule.heal_horizon == 604800
        assert cfg.retry.auth_floor == 3600
        assert cfg.mqtt.port == 1883
        assert cfg.mqtt.qos == 1
        assert cfg.mqtt.tls.enabled is False
        assert cfg.logging.level == "info"

    def test_unknown_key_fails_with_path(self, tmp_path):
        text = MINIMAL_CONFIG + "\n[schedule]\njiter = 300\n"
        with pytest.raises(gb.ConfigError, match="schedule.jiter"):
            load_cfg(tmp_path, text)

    def test_unknown_section_fails(self, tmp_path):
        text = MINIMAL_CONFIG + "\n[schedle]\ninterval = 600\n"
        with pytest.raises(gb.ConfigError, match="schedle"):
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
        text = "[mqtt]\nhost = \"broker.local\"\n"
        with pytest.raises(gb.ConfigError, match="glowmarkt.username"):
            load_cfg(tmp_path, text)

    def test_env_overrides_file_secrets(self, tmp_path):
        env = {
            gb.ENV_GLOW_USERNAME: "env@example.com",
            gb.ENV_GLOW_PASSWORD: "env-pass",
            gb.ENV_MQTT_PASSWORD: "env-mqtt",
        }
        cfg = load_cfg(tmp_path, MINIMAL_CONFIG, env)
        assert cfg.glowmarkt.username == "env@example.com"
        assert cfg.glowmarkt.password == "env-pass"
        assert cfg.mqtt.password == "env-mqtt"

    def test_env_only_credentials_suffice(self, tmp_path):
        text = "[mqtt]\nhost = \"broker.local\"\n"
        env = {
            gb.ENV_GLOW_USERNAME: "env@example.com",
            gb.ENV_GLOW_PASSWORD: "env-pass",
        }
        cfg = load_cfg(tmp_path, text, env)
        assert cfg.glowmarkt.password == "env-pass"

    def test_unknown_schedule_key_rejected(self, tmp_path):
        text = MINIMAL_CONFIG + "\n[schedule]\ntimezone = \"Europe/London\"\n"
        with pytest.raises(gb.ConfigError, match="unknown configuration key"):
            load_cfg(tmp_path, text)

    def test_interval_minimum_enforced(self, tmp_path):
        text = MINIMAL_CONFIG + "\n[schedule]\ninterval = 60\n"
        with pytest.raises(gb.ConfigError, match="minimum is 300"):
            load_cfg(tmp_path, text)

    def test_heal_horizon_must_exceed_finalisation_lag(self, tmp_path):
        text = MINIMAL_CONFIG + (
            "\n[schedule]\nfinalisation_lag = 5400\nheal_horizon = 5400\n"
        )
        with pytest.raises(gb.ConfigError, match="heal_horizon"):
            load_cfg(tmp_path, text)

    def test_tls_cert_requires_key(self, tmp_path):
        text = MINIMAL_CONFIG + (
            "\n[mqtt.tls]\nenabled = true\nclient_cert = \"/a.pem\"\n"
        )
        with pytest.raises(gb.ConfigError, match="client_cert and client_key"):
            load_cfg(tmp_path, text)

    def test_tls_options_without_enable_fail(self, tmp_path):
        text = MINIMAL_CONFIG + "\n[mqtt.tls]\nca_cert = \"/ca.pem\"\n"
        with pytest.raises(gb.ConfigError, match="tls.enabled is false"):
            load_cfg(tmp_path, text)

    def test_secretful_readable_config_warns(self, tmp_path, caplog):
        path = write_config(tmp_path, MINIMAL_CONFIG)
        path.chmod(0o644)
        with caplog.at_level("WARNING"):
            gb.load_config(path, environ={})
        assert any("group/world readable" in r.message for r in caplog.records)

    def test_secretless_readable_config_does_not_warn(self, tmp_path, caplog):
        text = "[mqtt]\nhost = \"broker.local\"\n"
        path = write_config(tmp_path, text)
        path.chmod(0o644)
        env = {
            gb.ENV_GLOW_USERNAME: "env@example.com",
            gb.ENV_GLOW_PASSWORD: "env-pass",
        }
        with caplog.at_level("WARNING"):
            gb.load_config(path, environ=env)
        assert not any(
            "group/world readable" in r.message for r in caplog.records
        )


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------


class TestScheduling:
    def test_offset_deterministic_and_bounded(self):
        a = gb.install_offset("machine-aaaa", 300)
        b = gb.install_offset("machine-aaaa", 300)
        c = gb.install_offset("machine-bbbb", 300)
        assert a == b
        assert 0 <= a <= 300
        assert 0 <= c <= 300

    def test_offset_zero_jitter(self):
        assert gb.install_offset("anything", 0) == 0

    def test_next_cycle_is_in_future_and_anchored(self):
        interval, offset = 1800, 137
        now = 1_752_000_000.0
        nxt = gb.next_cycle_start(now, interval, offset)
        assert nxt > now
        assert int(nxt - offset) % interval == 0
        assert nxt - now <= interval

    def test_next_cycle_exactly_on_boundary_moves_forward(self):
        interval, offset = 1800, 0
        now = float(1_752_000_000 - 1_752_000_000 % interval)
        nxt = gb.next_cycle_start(now, interval, offset)
        assert nxt == now + interval


# --------------------------------------------------------------------------
# Time handling
# --------------------------------------------------------------------------


class TestInitialWatermark:
    def test_seeds_24h_before_now_half_hour_aligned(self):
        now = datetime(2026, 7, 20, 14, 47, tzinfo=UTC)
        assert gb.initial_watermark(now) == datetime(
            2026, 7, 19, 14, 30, tzinfo=UTC
        )

    def test_independent_of_dst_transition(self):
        # Fixed 24h lookback, not civil-day arithmetic: no wall-clock jump
        # around a DST boundary.
        now = datetime(2026, 3, 30, 1, 0, tzinfo=UTC)
        assert gb.initial_watermark(now) == datetime(
            2026, 3, 29, 1, 0, tzinfo=UTC
        )


class TestFloorHalfHour:
    def test_floor_half_hour(self):
        dt = datetime(2026, 7, 20, 14, 47, 12, tzinfo=UTC)
        assert gb.floor_half_hour(dt) == datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
        aligned = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
        assert gb.floor_half_hour(aligned) == aligned


# --------------------------------------------------------------------------
# Emission
# --------------------------------------------------------------------------


def make_readings(start: datetime, values: list[float | None]) -> list[gb.Reading]:
    out = []
    for i, value in enumerate(values):
        if value is None:
            continue
        out.append(gb.Reading(start=start + i * HH, kwh=value))
    return out


# A horizon large enough that no window is ever abandoned; tests that care
# about abandonment pass their own small value.
NEVER_HEAL = int(timedelta(days=3650).total_seconds())


def emit(readings, watermark, now, lag=0, emitted=None, heal_horizon=NEVER_HEAL):
    return gb.select_emittable(
        readings, watermark, set(emitted or ()), now, lag, heal_horizon
    )


class TestSelectEmittable:
    WM = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    def test_emits_contiguous_finalised_intervals(self):
        readings = make_readings(self.WM, [0.5, 0.25, 0.25])
        now = self.WM + 3 * HH + timedelta(seconds=5400)
        e = emit(readings, self.WM, now, lag=5400)
        assert e.intervals == 3
        assert e.added_wh == 1000
        assert e.new_watermark == self.WM + 3 * HH
        assert e.new_emitted == set()  # contiguous: ledger stays empty

    def test_finalisation_lag_holds_back_recent_intervals(self):
        readings = make_readings(self.WM, [0.5, 0.25, 0.25])
        now = self.WM + 3 * HH  # third interval just ended; lag not met
        e = emit(readings, self.WM, now, lag=5400)
        assert e.intervals == 0
        assert e.new_watermark == self.WM

    def test_gap_does_not_halt_emission_but_holds_frontier(self):
        # The window after the gap is emitted immediately (into the ledger),
        # but the contiguous frontier stays at the gap until it heals.
        readings = make_readings(self.WM, [0.5, None, 0.25, 0.25])
        now = self.WM + 10 * HH
        e = emit(readings, self.WM, now)
        assert e.intervals == 3
        assert e.added_wh == 1000  # 500 + 250 + 250, gap excluded
        assert e.new_watermark == self.WM + HH  # frontier held at the gap
        assert e.new_emitted == {self.WM + 2 * HH, self.WM + 3 * HH}

    def test_late_arrival_heals_exactly_once_across_cycles(self):
        # Cycle 1: a gap at WM+HH; windows either side are emitted and the
        # one above the gap goes into the ledger.
        first = emit(make_readings(self.WM, [0.5, None, 0.3]), self.WM,
                     self.WM + 3 * HH)
        assert first.intervals == 2
        assert first.added_wh == 800
        assert first.new_watermark == self.WM + HH
        assert first.new_emitted == {self.WM + 2 * HH}

        # Cycle 2: the missing window finally arrives. It is emitted, the
        # frontier sweeps the whole contiguous region and the ledger drains.
        # The already-emitted WM+2HH is NOT counted again.
        second = emit(
            make_readings(self.WM, [0.5, 0.4, 0.3]),
            first.new_watermark,
            self.WM + 3 * HH,
            emitted=first.new_emitted,
        )
        assert second.intervals == 1  # only the healed window
        assert second.added_wh == 400  # exactly-once: 0.4 kWh, not the 0.3 again
        assert second.new_watermark == self.WM + 3 * HH
        assert second.new_emitted == set()
        # Total across both cycles equals the sum of every window, once each.
        assert first.added_wh + second.added_wh == 500 + 400 + 300

    def test_gap_abandoned_once_past_heal_horizon(self):
        # The gap at WM is older than the horizon, so the frontier steps over
        # it (accepting the lost half-hour) rather than stalling forever.
        readings = make_readings(self.WM, [None, 0.1, 0.1, 0.1, 0.1])
        now = self.WM + 5 * HH
        e = emit(readings, self.WM, now, heal_horizon=int(4 * HH.total_seconds()))
        assert e.intervals == 4
        assert e.added_wh == 400
        assert e.new_watermark == self.WM + 5 * HH  # swept past the abandoned gap
        assert e.new_emitted == set()

    def test_gap_within_horizon_is_not_abandoned(self):
        # Same shape, but the gap is younger than the horizon: the frontier
        # waits for it rather than abandoning it.
        readings = make_readings(self.WM, [None, 0.1, 0.1, 0.1, 0.1])
        now = self.WM + 5 * HH
        e = emit(readings, self.WM, now, heal_horizon=int(100 * HH.total_seconds()))
        assert e.new_watermark == self.WM  # frontier held at the gap
        assert e.new_emitted == {
            self.WM + HH, self.WM + 2 * HH, self.WM + 3 * HH, self.WM + 4 * HH
        }

    def test_readings_behind_watermark_ignored(self):
        readings = make_readings(self.WM - 2 * HH, [9.9, 9.9, 0.5])
        now = self.WM + 5 * HH
        e = emit(readings, self.WM, now)
        assert e.intervals == 1
        assert e.added_wh == 500
        assert e.new_watermark == self.WM + HH

    def test_no_readings_no_movement(self):
        now = self.WM + 5 * HH
        e = emit([], self.WM, now)
        assert e.intervals == 0
        assert e.added_wh == 0
        assert e.new_watermark == self.WM
        assert e.new_emitted == set()

    def test_unsorted_input_handled(self):
        # The ledger model is order-independent by construction; unsorted
        # input must still emit each window exactly once.
        readings = list(reversed(make_readings(self.WM, [0.1, 0.2, 0.3])))
        now = self.WM + 10 * HH
        e = emit(readings, self.WM, now)
        assert e.intervals == 3
        assert e.added_wh == 600
        assert e.new_watermark == self.WM + 3 * HH

    def test_rounding_accumulates_in_integer_wh(self):
        # 0.0005 kWh rounds to 1 Wh (banker's rounding is acceptable but the
        # accumulation must stay integral).
        readings = make_readings(self.WM, [0.0004, 0.0004])
        now = self.WM + 10 * HH
        e = emit(readings, self.WM, now)
        assert isinstance(e.added_wh, int)
        assert e.added_wh == 0

    def test_spring_forward_day_has_46_slots(self):
        # 2026-03-29: Europe/London's civil day is 23h/46 half-hour slots
        # long. select_emittable works in UTC intervals throughout, so it
        # must still emit every one of them without special-casing DST.
        day_start = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)
        readings = make_readings(day_start, [0.1] * 46)
        now = day_start + timedelta(hours=23) + timedelta(hours=2)
        e = emit(readings, day_start, now)
        assert e.intervals == 46
        assert e.new_watermark == datetime(2026, 3, 29, 23, 0, tzinfo=UTC)

    def test_fall_back_day_has_50_slots(self):
        # 2026-10-25: civil day is 25h/50 half-hour slots long.
        day_start = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)
        readings = make_readings(day_start, [0.1] * 50)
        now = day_start + timedelta(hours=25) + timedelta(hours=2)
        e = emit(readings, day_start, now)
        assert e.intervals == 50
        assert e.new_watermark == datetime(2026, 10, 26, 0, 0, tzinfo=UTC)


class TestFetchWindows:
    def test_single_window_for_short_span(self):
        start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
        end = start + timedelta(hours=6)
        assert gb.fetch_windows(start, end) == [(start, end)]

    def test_long_span_chunked_and_covering(self):
        start = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
        end = start + timedelta(days=23)
        windows = gb.fetch_windows(start, end)
        assert windows[0][0] == start
        assert windows[-1][1] == end
        for (a_start, a_end), (b_start, _) in zip(windows, windows[1:]):
            assert a_end == b_start
            assert a_end - a_start <= gb.MAX_FETCH_SPAN

    def test_empty_span(self):
        start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
        assert gb.fetch_windows(start, start) == []


# --------------------------------------------------------------------------
# State file
# --------------------------------------------------------------------------


class TestStateFile:
    def test_round_trip(self, tmp_path):
        state = gb.StateFile(tmp_path)
        wm = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
        ledger = {wm + 2 * HH, wm + 3 * HH}
        state.upsert_resource("res-1", "electricity.consumption", "Elec", wm)
        state.advance("res-1", wm + HH, ledger, 500)
        state.set_token("secret-token", datetime.now(tz=UTC))
        state.save()

        reloaded = gb.StateFile(tmp_path)
        reloaded.load()
        assert reloaded.watermark("res-1") == wm + HH
        assert reloaded.emitted("res-1") == ledger
        assert reloaded.cumulative_wh("res-1") == 500
        assert reloaded.token == "secret-token"

    def test_v1_state_migrates_to_v2(self, tmp_path):
        # A v1 file (no per-resource ledger) must upgrade in place, keeping
        # its cumulative total so no spurious meter-reset is emitted, and
        # gain an empty ledger (a v1 watermark had nothing emitted above it).
        wm = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
        v1 = {
            "schema": 1,
            "auth": {"token": "t", "obtained_at": "", "last_attempt_at": ""},
            "resources": {
                "res-1": {
                    "classifier": "electricity.consumption",
                    "name": "Elec",
                    "watermark": wm.isoformat(),
                    "cumulative_wh": 1234,
                }
            },
        }
        state = gb.StateFile(tmp_path)
        state.path.parent.mkdir(parents=True, exist_ok=True)
        state.path.write_text(json.dumps(v1), encoding="utf-8")
        state.load()

        assert state.data["schema"] == 2
        assert state.watermark("res-1") == wm
        assert state.emitted("res-1") == set()
        assert state.cumulative_wh("res-1") == 1234
        assert state.token == "t"

    def test_corrupt_state_starts_fresh(self, tmp_path, caplog):
        state = gb.StateFile(tmp_path)
        state.path.parent.mkdir(parents=True, exist_ok=True)
        state.path.write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            state.load()
        assert state.data["resources"] == {}
        assert any("corrupt" in r.message for r in caplog.records)

    def test_unknown_schema_starts_fresh(self, tmp_path):
        state = gb.StateFile(tmp_path)
        state.path.parent.mkdir(parents=True, exist_ok=True)
        state.path.write_text(json.dumps({"schema": 99}), encoding="utf-8")
        state.load()
        assert state.data["schema"] == gb.STATE_SCHEMA

    def test_missing_file_is_fresh(self, tmp_path):
        state = gb.StateFile(tmp_path / "nowhere")
        state.load()
        assert state.data["resources"] == {}

    def test_atomic_write_leaves_no_tmp(self, tmp_path):
        state = gb.StateFile(tmp_path)
        state.save()
        leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


class TestRedaction:
    def _render(self, msg: str, secrets: list[str]) -> str:
        fmt = gb.RedactingFormatter("%(message)s", secrets=secrets)
        record = logging.LogRecord(
            "glowbridge", logging.DEBUG, __file__, 1, msg, None, None
        )
        return fmt.format(record)

    def test_secret_values_scrubbed(self):
        out = self._render("password is hunter2 ok", ["hunter2"])
        assert "hunter2" not in out
        assert "***" in out

    def test_json_token_fields_masked(self):
        payload = '{"token": "eyJhbGciOi.abc.def", "valid": true}'
        out = self._render(payload, [])
        assert "eyJhbGciOi" not in out
        assert '"token": "***"' in out

    def test_json_mode_still_redacts(self):
        fmt = gb.RedactingFormatter("%(message)s", secrets=["s3cret"], json_mode=True)
        record = logging.LogRecord(
            "glowbridge", logging.INFO, __file__, 1, "value s3cret here", None, None
        )
        out = fmt.format(record)
        assert "s3cret" not in out
        json.loads(out)


# --------------------------------------------------------------------------
# Cycle wiring with fakes
# --------------------------------------------------------------------------


class FakeClient:
    """Stands in for GlowmarktClient: canned resources/readings, scripted
    failures."""

    def __init__(self, readings_by_resource=None, resources=None):
        self.readings_by_resource = readings_by_resource or {}
        self.resources = resources or []
        self.token = ""
        self.auth_calls = 0
        self.fail_auth = False
        self.fail_readings: list[Exception] = []

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
        pass

    def get_readings(self, resource_id, start, end):
        if self.fail_readings:
            raise self.fail_readings.pop(0)
        return [
            r
            for r in self.readings_by_resource.get(resource_id, [])
            if start <= r.start < end
        ]


class FakePublisher:
    def __init__(self):
        self.published: list[tuple[str, str, bool]] = []

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))

    def topics(self):
        return [t for t, _, _ in self.published]

    def payloads(self, topic):
        """Every payload published to *topic*, in order."""
        return [p for t, p, _ in self.published if t == topic]

    def payload(self, topic):
        """The most recent payload published to *topic*."""
        return self.payloads(topic)[-1]


def make_bridge(tmp_path, client, publisher, **cfg_overrides):
    cfg = gb.Config(
        glowmarkt=gb.GlowmarktConfig(
            username="u", password="p", **cfg_overrides.get("glowmarkt", {})
        ),
        schedule=gb.ScheduleConfig(
            finalisation_lag=0, **cfg_overrides.get("schedule", {})
        ),
        retry=gb.RetryConfig(auth_floor=0),
    )
    state = gb.StateFile(tmp_path)
    state.load()
    return gb.Bridge(cfg, state, client, publisher), state


class TestCycle:
    WM = datetime(2026, 7, 19, 23, 0, tzinfo=UTC)  # arbitrary fixed watermark

    @pytest.fixture(autouse=True)
    def _fixed_seed(self, monkeypatch):
        """Pin the fresh-state seed so cycles start from a known watermark
        instead of 24 hours before wall-clock now."""
        monkeypatch.setattr(gb, "initial_watermark", lambda now: self.WM)

    def _client_with_data(self):
        readings = make_readings(self.WM, [0.5, 0.25, 0.25, 0.5])
        return FakeClient(
            readings_by_resource={"res-elec": readings},
            resources=[
                {
                    "resourceId": "res-elec",
                    "classifier": "electricity.consumption",
                    "name": "electricity consumption",
                },
                {
                    "resourceId": "res-cost",
                    "classifier": "electricity.consumption.cost",
                    "name": "electricity cost",
                },
            ],
        )

    def test_first_cycle_discovers_emits_and_publishes(self, tmp_path):
        client = self._client_with_data()
        publisher = FakePublisher()
        bridge, state = make_bridge(tmp_path, client, publisher)

        now = self.WM + 4 * HH
        bridge.run_cycle(now)

        assert state.resource_ids() == ["res-elec"]  # cost resource filtered
        assert state.cumulative_wh("res-elec") == 1500
        assert state.watermark("res-elec") == self.WM + 4 * HH

        topics = publisher.topics()
        assert "glowbridge/res-elec/state" in topics
        assert "glowbridge/res-elec/status" in topics
        assert "glowbridge/bridge/status" in topics

        assert publisher.payload("glowbridge/res-elec/state") == "1.500"

        status = json.loads(publisher.payload("glowbridge/res-elec/status"))
        assert status["cumulative_kwh"] == 1.5
        assert status["data_complete_to"] == (self.WM + 4 * HH).isoformat()
        assert status["consecutive_failures"] == 0

    def test_second_cycle_emits_only_new_intervals(self, tmp_path):
        client = self._client_with_data()
        publisher = FakePublisher()
        bridge, state = make_bridge(tmp_path, client, publisher)

        bridge.run_cycle(self.WM + 4 * HH)
        publisher.published.clear()

        client.readings_by_resource["res-elec"] = make_readings(
            self.WM, [0.5, 0.25, 0.25, 0.5, 1.0]
        )
        bridge.run_cycle(self.WM + 5 * HH)
        assert state.cumulative_wh("res-elec") == 2500
        assert publisher.payloads("glowbridge/res-elec/state") == ["2.500"]

    def test_no_new_data_publishes_status_not_state(self, tmp_path):
        client = self._client_with_data()
        publisher = FakePublisher()
        bridge, state = make_bridge(tmp_path, client, publisher)

        bridge.run_cycle(self.WM + 4 * HH)
        publisher.published.clear()
        bridge.run_cycle(self.WM + 4 * HH)  # same now; nothing new

        topics = publisher.topics()
        assert "glowbridge/res-elec/state" not in topics
        assert "glowbridge/res-elec/status" in topics

    def test_state_written_before_publish(self, tmp_path):
        client = self._client_with_data()

        class ExplodingPublisher(FakePublisher):
            def publish(self, topic, payload, retain=False):
                raise gb.CycleError("broker gone")

        bridge, state = make_bridge(tmp_path, client, ExplodingPublisher())
        with pytest.raises(gb.CycleError):
            bridge.run_cycle(self.WM + 4 * HH)

        reloaded = gb.StateFile(tmp_path)
        reloaded.load()
        assert reloaded.cumulative_wh("res-elec") == 1500

    def test_abandoned_gap_advance_is_persisted(self, tmp_path):
        # A cycle can move the frontier without emitting anything, when a gap
        # ages past the heal horizon. That advance must reach disk: if only
        # resources with new energy were persisted, the abandonment would be
        # recomputed every cycle and the frontier would never settle.
        client = FakeClient(
            resources=[
                {
                    "resourceId": "res-elec",
                    "classifier": "electricity.consumption",
                    "name": "elec",
                }
            ]
        )  # no readings at all: nothing is emittable
        bridge, state = make_bridge(
            tmp_path,
            client,
            FakePublisher(),
            schedule={"heal_horizon": int(2 * HH.total_seconds())},
        )

        bridge.run_cycle(self.WM + 10 * HH)

        # Everything up to now-heal_horizon is written off; nothing emitted.
        assert state.cumulative_wh("res-elec") == 0
        assert state.watermark("res-elec") == self.WM + 8 * HH

        reloaded = gb.StateFile(tmp_path)
        reloaded.load()
        assert reloaded.watermark("res-elec") == self.WM + 8 * HH

    def test_token_expiry_reauths_once_and_continues(self, tmp_path):
        client = self._client_with_data()
        client.fail_readings = [gb.TokenExpired()]
        publisher = FakePublisher()
        bridge, state = make_bridge(tmp_path, client, publisher)
        state.set_token("stale", datetime.now(tz=UTC))

        bridge.run_cycle(self.WM + 4 * HH)
        assert client.auth_calls == 1
        assert state.cumulative_wh("res-elec") == 1500

    def test_pinned_resource_404_is_fatal(self, tmp_path):
        client = FakeClient()
        client.fail_readings = [gb.ResourceMissing("res-pinned")]
        publisher = FakePublisher()
        bridge, state = make_bridge(
            tmp_path,
            client,
            publisher,
            glowmarkt={"resources": ("res-pinned",)},
        )
        with pytest.raises(gb.CycleError, match="pinned resource"):
            bridge.run_cycle(self.WM + 4 * HH)

    def test_cached_resource_404_triggers_rediscovery(self, tmp_path):
        client = self._client_with_data()
        publisher = FakePublisher()
        bridge, state = make_bridge(tmp_path, client, publisher)
        state.upsert_resource(
            "res-dead", "electricity.consumption", "old meter", self.WM
        )

        client.fail_readings = [gb.ResourceMissing("res-dead")]
        bridge.run_cycle(self.WM + 4 * HH)
        assert state.resource("res-dead") is None
        assert "res-elec" in state.resource_ids()

    def test_auth_floor_blocks_reauth(self, tmp_path):
        client = FakeClient()
        bridge, state = make_bridge(tmp_path, client, FakePublisher())
        bridge.cfg = gb.Config(
            glowmarkt=gb.GlowmarktConfig(username="u", password="p"),
            retry=gb.RetryConfig(auth_floor=3600),
        )
        now = datetime.now(tz=UTC)
        state.mark_auth_attempt(now - timedelta(seconds=60))
        with pytest.raises(gb.AuthError, match="floor not met"):
            bridge.ensure_token(now)
        assert client.auth_calls == 0


class TestRetryOrchestration:
    def _bridge_that_fails(self, tmp_path, times: int):
        client = FakeClient(
            resources=[
                {
                    "resourceId": "res-1",
                    "classifier": "electricity.consumption",
                    "name": "elec",
                }
            ]
        )
        client.fail_readings = [
            gb.CycleError(f"boom {i}") for i in range(times)
        ]
        bridge, _ = make_bridge(tmp_path, client, FakePublisher())
        return bridge

    def test_retries_then_succeeds(self, tmp_path):
        bridge = self._bridge_that_fails(tmp_path, times=2)
        sleeps = []
        cfg = bridge.cfg
        ok = gb.run_cycle_with_retries(bridge, cfg, sleeper=sleeps.append)
        assert ok
        assert len(sleeps) == 2
        assert bridge.consecutive_failures == 0

    def test_budget_exhaustion_returns_false(self, tmp_path):
        bridge = self._bridge_that_fails(tmp_path, times=99)
        ok = gb.run_cycle_with_retries(bridge, bridge.cfg, sleeper=lambda s: None)
        assert not ok
        assert bridge.consecutive_failures == 1

    def test_auth_error_short_circuits(self, tmp_path):
        client = FakeClient()
        client.fail_auth = True
        bridge, _ = make_bridge(tmp_path, client, FakePublisher())
        sleeps = []
        ok = gb.run_cycle_with_retries(bridge, bridge.cfg, sleeper=sleeps.append)
        assert not ok
        assert sleeps == []
        assert client.auth_calls == 1

    def test_rate_limit_respects_retry_after(self, tmp_path):
        bridge = self._bridge_that_fails(tmp_path, times=0)
        bridge.client.fail_readings = [gb.RateLimited(retry_after=120)]
        sleeps = []
        ok = gb.run_cycle_with_retries(bridge, bridge.cfg, sleeper=sleeps.append)
        assert ok
        assert sleeps == [120]


# --------------------------------------------------------------------------
# Discovery payload
# --------------------------------------------------------------------------


class TestDiscovery:
    def test_payload_shape(self):
        cfg = gb.MqttConfig()
        topic, payload = gb.discovery_payload(
            cfg, "abc-123", "electricity.consumption", "Electricity consumption"
        )
        assert topic == "homeassistant/sensor/glowbridge_abc_123/config"
        body = json.loads(payload)
        assert body["state_class"] == "total_increasing"
        assert body["device_class"] == "energy"
        assert body["unit_of_measurement"] == "kWh"
        assert body["state_topic"] == "glowbridge/abc-123/state"
        assert body["json_attributes_topic"] == "glowbridge/abc-123/status"
        assert body["unique_id"] == "glowbridge_abc_123"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
