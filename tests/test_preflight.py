"""Тесты pre-flight оценки (drone_core.preflight.evaluate).

Запуск без pytest:  python tests/test_preflight.py
Или под pytest:     pytest tests/test_preflight.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from drone_core import preflight  # noqa: E402

_HEALTHY = {
    "gyro_ok": True, "accel_ok": True, "mag_ok": True,
    "local_pos_ok": True, "global_pos_ok": True, "home_ok": True, "armable": True,
}
_GPS_OK = {"satellites": 14, "fix_type": "FIX_TYPE_3D"}
_BAT_OK = {"pct": 82.0, "voltage_v": 16.4}


def test_all_green_passes():
    r = preflight.evaluate(_HEALTHY, _GPS_OK, _BAT_OK)
    assert r["ok"] is True
    assert r["verdict"] == "PASSED"
    assert r["reasons"] == []
    assert any(c["key"] == "armable" and c["status"] == "ok" for c in r["checks"])


def test_empty_is_failsafe_blocked():
    # Нет данных → армиться нельзя (fail-safe).
    r = preflight.evaluate({}, {}, {})
    assert r["ok"] is False
    assert r["verdict"] == "BLOCKED"
    assert len(r["reasons"]) > 0


def test_no_gps_blocks():
    # Реалистичный «нет GPS»: без фикса PX4 не даёт global/home/armable →
    # блокируется по авторитетным пунктам (GPS-пункт сам по себе — warn/инфо).
    h = dict(_HEALTHY, global_pos_ok=False, home_ok=False, armable=False)
    r = preflight.evaluate(h, {"satellites": 0, "fix_type": "NO_GPS"}, _BAT_OK)
    assert r["ok"] is False
    assert any(("GLOBAL" in x or "HOME" in x or "ARMABLE" in x) for x in r["reasons"])


def test_low_battery_blocks():
    r = preflight.evaluate(_HEALTHY, _GPS_OK, {"pct": 12.0, "voltage_v": 13.5})
    assert r["ok"] is False
    assert any("BATTERY" in reason for reason in r["reasons"])


def test_not_armable_blocks():
    h = dict(_HEALTHY, armable=False)
    r = preflight.evaluate(h, _GPS_OK, _BAT_OK)
    assert r["ok"] is False
    assert any("ARMABLE" in reason for reason in r["reasons"])


def test_missing_calibration_blocks():
    h = dict(_HEALTHY, mag_ok=False)
    r = preflight.evaluate(h, _GPS_OK, _BAT_OK)
    assert r["ok"] is False
    assert any("MAG" in reason for reason in r["reasons"])


def test_gps_partial_is_warn_not_block_on_that_item():
    # Есть спутники, но нет 3D-фикса → это blocker (нужен фикс), но статус пункта warn/bad.
    r = preflight.evaluate(_HEALTHY, {"satellites": 4, "fix_type": "FIX_TYPE_2D"}, _BAT_OK)
    gps_check = next(c for c in r["checks"] if c["key"] == "gps")
    assert gps_check["status"] in ("warn", "bad")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
