import math

import pytest

from drone_core import preflight
from drone_core.real_safety import RealSafety, expire_report, PARAM_RULES, battery_percent


HEALTH = {
    "gyro_ok": True,
    "accel_ok": True,
    "mag_ok": True,
    "local_pos_ok": True,
    "global_pos_ok": True,
    "home_ok": True,
    "armable": True,
}
PARAMS = {
    "MAV_TYPE": 2,
    "COM_OBL_RC_ACT": 4,
    "COM_OF_LOSS_T": 1.0,
    "NAV_DLL_ACT": 3,
    "COM_DL_LOSS_T": 3,
    "COM_RC_OVERRIDE": 3,
    "COM_RC_IN_MODE": 0,
    "COM_DISARM_LAND": 2,
    "MPC_XY_VEL_MAX": 0.8,
    "MPC_TKO_SPEED": 0.5,
    "MPC_LAND_SPEED": 0.3,
}


def feed(s, armed=False, airborne=False):
    s.observe("armed", armed)
    s.observe("connection", True)
    s.observe("health", dict(HEALTH, armable=not armed))
    s.observe("gps", {"satellites": 17, "fix_type": "FIX_3D"})
    s.observe("battery", {"pct": 93, "voltage_v": 16.4})
    s.observe("position", {"lat": 44.5, "lon": -87.9, "alt": 1 if airborne else 0})
    s.observe("ned", {"n": 12.0, "e": -5.0, "d": -1.0 if airborne else 0.0, "vn": 0, "ve": 0, "vd": 0})
    s.observe("attitude", {"roll": 0, "pitch": 0, "yaw": 90})
    s.observe("armed", armed)
    s.observe("in_air", airborne)
    s.observe("landed", "IN_AIR" if airborne else "ON_GROUND")
    s.observe("mode", "OFFBOARD" if airborne else "HOLD")
    s.observe("rc", {"available": True, "signal": 90})
    s.observe("params", dict(PARAMS))


def ready():
    now = [100.0]
    s = RealSafety(clock=lambda: now[0])
    feed(s)
    assert not s.report()["ok"]
    for _ in range(10):
        now[0] += 1
        feed(s)
        s.report()
    assert s.report()["can_arm"]
    return s, now


def test_ready_requires_continuous_good_data_and_resets_on_one_mag_failure():
    s, now = ready()
    s.observe("health", dict(HEALTH, mag_ok=False, armable=False))
    # A brief false→true between polls must still reset the stability timer.
    s.observe("health", dict(HEALTH))
    assert not s.report()["can_arm"]
    for _ in range(9):
        now[0] += 1
        feed(s)
        assert not s.report()["can_arm"]
    now[0] += 1
    feed(s)
    assert s.report()["can_arm"]


@pytest.mark.parametrize("key", ["position", "ned", "attitude", "battery", "gps"])
def test_each_required_stream_expires_independently(key):
    s, now = ready()
    stale = s.seen[key]
    now[0] += 3.01
    feed(s)
    s.seen[key] = stale
    report = s.report()
    assert not report["ok"]
    assert not report["can_arm"]


@pytest.mark.parametrize("key", ["health", "landed", "armed", "in_air", "mode"])
def test_closed_state_subscription_revokes_permission(key):
    s, _ = ready()
    s.stream_failed(key)
    assert not s.report()["ok"]


def test_unchanged_state_is_valid_only_with_live_subscription_and_dynamic_link():
    s, now = ready()
    stamps = {key: s.seen[key] for key in ("health", "landed", "armed", "in_air", "mode")}
    for _ in range(5):
        now[0] += 1
        feed(s)
    s.seen.update(stamps)
    assert s.report()["ok"]
    s.observe("connection", False)
    assert not s.report()["ok"]


def test_loss_of_connection_revokes_readiness_immediately():
    s, _ = ready()
    s.observe("connection", False)
    assert not s.report()["ok"]
    assert not s.report()["link_connected"]


@pytest.mark.parametrize(
    "battery",
    [
        {},
        {"pct": None, "voltage_v": 16.4},
        {"pct": 29, "voltage_v": 16.4},
        {"pct": math.nan, "voltage_v": 16.4},
        {"pct": 93, "voltage_v": math.nan},
    ],
)
def test_unknown_invalid_or_low_battery_blocks(battery):
    assert not preflight.evaluate(HEALTH, {"satellites": 17, "fix_type": "FIX_3D"}, battery)["ok"]


def test_2d_gps_and_missing_armable_are_hard_blockers_even_if_other_flags_green():
    battery = {"pct": 90, "voltage_v": 16.4}
    assert not preflight.evaluate(HEALTH, {"satellites": 5, "fix_type": "FIX_2D"}, battery)["ok"]
    assert not preflight.evaluate(
        {k: v for k, v in HEALTH.items() if k != "armable"}, {"satellites": 17, "fix_type": "FIX_3D"}, battery
    )["ok"]


@pytest.mark.parametrize("value", [math.nan, math.inf, None])
def test_invalid_local_position_never_grants_permission(value):
    s, _ = ready()
    s.observe("ned", dict(s.values["ned"], n=value))
    assert not s.report()["ok"]


def test_inflight_preflight_does_not_require_prearm_permission():
    s, _ = ready()
    feed(s, armed=True, airborne=True)
    assert s.report()["ok"]
    assert not s.report()["can_disarm"]
    assert not s.report()["can_arm"]


def test_uncertain_arm_transition_requires_a_new_stable_interval():
    s, now = ready()
    s.observe("health", dict(HEALTH, armable=False))
    s.observe("armed", True)
    assert not s.report()["ok"]
    for _ in range(10):
        now[0] += 1
        feed(s, armed=True)
        s.report()
    assert s.report()["ok"]


def test_sub_meter_flight_does_not_count_as_landed():
    s, _ = ready()
    feed(s, armed=True, airborne=True)
    s.observe("position", dict(s.values["position"], alt=0.2))
    assert not s.report()["can_disarm"]


@pytest.mark.parametrize("parameter", list(PARAM_RULES))
def test_missing_onboard_safeguard_blocks_flight(parameter):
    s, _ = ready()
    assert s.report()["flight_ready"]
    s.observe("params", {k: v for k, v in PARAMS.items() if k != parameter})
    assert not s.report()["flight_ready"]


def test_rc_loss_blocks_flight():
    s, _ = ready()
    s.observe("rc", {"available": False, "signal": 0})
    assert not s.report()["flight_ready"]


def test_dead_bridge_report_expires_even_when_previous_flags_are_green():
    report = dict(ok=True, can_arm=True, can_disarm=True, can_move=True, flight_ready=True, link_connected=True)
    expired = expire_report(report, 100, now=104)
    for key in report:
        assert expired[key] is False
    assert report["ok"]  # caller's cached report is not mutated


@pytest.mark.parametrize(
    "raw, expected",
    [
        (1, 1.0),
        (0.5, 0.5),
        (93, 93.0),
        (100, 100.0),
        (-1, None),
        (math.nan, None),
        (math.inf, None),
        (None, None),
        (101, None),
    ],
)
def test_pinned_sdk_battery_scale_does_not_turn_low_charge_into_full(raw, expected):
    assert battery_percent(raw) == expected
