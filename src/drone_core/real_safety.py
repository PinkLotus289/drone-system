"""Real-airframe readiness. Only observed telemetry can grant permission.

Monotonic timestamps never cross a process boundary. The web process receives
an expiring report; command permission is checked again inside the bridge.
"""

from __future__ import annotations

import math
import time
import uuid

from drone_core import preflight

FRESH_S = 3.0
STABLE_S = 10.0
MAX_ALT_M = 2.0
TEST_ALT_M = 1.0
MAX_RADIUS_M = 3.0
STEP_M = 0.5
SPEED_M_S = 0.25
FLIGHT_TIMEOUT_S = 45.0
OPERATOR_TIMEOUT_S = 4.0
STATE_STREAMS = frozenset({"health", "armed", "in_air", "landed", "mode", "rc"})

# Read, never write. These values are deliberately conservative for initial
# outdoor multicopter tests. Unknown/unsupported parameters block flight.
PARAM_RULES = {
    "MAV_TYPE": ("int", lambda v: v in (2, 13, 14, 15), "multicopter (2, 13, 14 or 15)"),
    "COM_OBL_RC_ACT": ("int", lambda v: v == 4, "4 (Land)"),
    "COM_OF_LOSS_T": ("float", lambda v: 0.1 <= v <= 2, "0.1–2 s"),
    "NAV_DLL_ACT": ("int", lambda v: v == 3, "3 (Land)"),
    "COM_DL_LOSS_T": ("int", lambda v: 1 <= v <= 5, "1–5 s"),
    "COM_RC_OVERRIDE": ("int", lambda v: int(v) & 3 == 3, "3 (RC override in Auto and Offboard)"),
    "COM_RC_IN_MODE": ("int", lambda v: v in (0, 2, 3), "RC input enabled (0, 2 or 3)"),
    "COM_DISARM_LAND": ("float", lambda v: 0 < v <= 5, "0–5 s, enabled"),
    "MPC_XY_VEL_MAX": ("float", lambda v: 0 < v <= 1, "at most 1 m/s"),
    "MPC_TKO_SPEED": ("float", lambda v: 0 < v <= 0.7, "at most 0.7 m/s"),
    "MPC_LAND_SPEED": ("float", lambda v: 0 < v <= 0.5, "at most 0.5 m/s"),
}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def battery_percent(value):
    """MAVSDK 2.8: [0,100]. Negative/NaN/unknown are never a full battery."""
    return round(float(value), 1) if finite(value) and 0 <= value <= 100 else None


class RealSafety:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.bridge_id = uuid.uuid4().hex
        self.values = {}
        self.seen = {}
        self.connected = False
        self.good_since = None
        self.status_text = []
        self.live_streams = set()

    def observe(self, key, value):
        self.values[key] = value
        self.seen[key] = self.clock()
        if key in STATE_STREAMS:
            self.live_streams.add(key)
        # Do not miss short failures between report/command evaluations.
        if key in {"health", "gps", "battery", "position", "ned", "attitude", "armed", "in_air", "landed"}:
            if any(c["status"] == "bad" for c in self.checks(airborne=self.values.get("armed") is True)):
                self.good_since = None
        if key == "connection":
            self.connected = value is True
            if not self.connected:
                self.good_since = None

    def fresh(self, key, limit=FRESH_S):
        # These SDK subscriptions emit on CHANGE. Their cached state remains
        # valid while subscribed and the independent live link/position is
        # fresh. Expiring an unchanged armed=False/health=True is incorrect.
        if key in STATE_STREAMS:
            return key in self.seen and key in self.live_streams and self.connected and self.fresh("position")
        return key in self.seen and 0 <= self.clock() - self.seen[key] <= limit

    def stream_failed(self, key):
        self.live_streams.discard(key)
        self.good_since = None

    def basic(self, airborne=False):
        health = dict(self.values.get("health") or {})
        # PX4's armable flag describes pre-arm permission, not in-flight health.
        if airborne:
            health["armable"] = True
        return preflight.evaluate(health, self.values.get("gps"), self.values.get("battery"))

    def checks(self, airborne=False):
        result = self.basic(airborne)
        checks = result["checks"]

        def add(key, label, ok, value):
            checks.append({"key": key, "label": label, "status": "ok" if ok else "bad", "value": value})

        add("connection", "MAVLINK LINK", self.connected, "connected" if self.connected else "lost / unknown")
        for key in ("position", "ned", "attitude", "gps", "battery", "health", "armed", "in_air", "landed", "mode"):
            age = self.clock() - self.seen.get(key, -1e9)
            label = key.upper() + (" STREAM" if key in STATE_STREAMS else " FRESHNESS")
            value = (
                f"live; last change {age:.1f} s ago"
                if self.fresh(key) and key in STATE_STREAMS
                else f"{age:.1f} s"
                if age < 1e8
                else "not received"
            )
            add("fresh_" + key, label, self.fresh(key), value)
        pos = self.values.get("position") or {}
        ned = self.values.get("ned") or {}
        attitude = self.values.get("attitude") or {}
        valid = all(finite(pos.get(k)) for k in ("lat", "lon", "alt"))
        valid = valid and -90 <= pos["lat"] <= 90 and -180 <= pos["lon"] <= 180
        valid = valid and all(finite(ned.get(k)) for k in ("n", "e", "d", "vn", "ve", "vd"))
        valid = valid and all(finite(attitude.get(k)) for k in ("roll", "pitch", "yaw"))
        add("numeric", "POSITION / ATTITUDE", bool(valid), "valid" if valid else "missing / invalid")
        health = self.values.get("health") or {}
        gps = self.values.get("gps") or {}
        battery = self.values.get("battery") or {}
        # The real-flight floor cannot be weakened by legacy SIM/env options.
        home_ok = health.get("global_pos_ok") is True and health.get("home_ok") is True
        add("real_home", "GLOBAL / HOME POSITION", home_ok, "valid" if home_ok else "required")
        pct = battery.get("pct")
        add("real_battery", "TEST BATTERY FLOOR", finite(pct) and 30 <= pct <= 100, f"{pct}% (minimum 30%)")
        sats = gps.get("satellites")
        add(
            "real_gps",
            "TEST GPS FLOOR",
            finite(sats) and sats >= 6 and preflight._fix_ok(gps.get("fix_type")),
            f"{gps.get('fix_type')} / {sats} satellites (minimum 6 + 3D fix)",
        )
        if not airborne:
            ground = self.values.get("in_air") is False and self.values.get("landed") == "ON_GROUND"
            add("ground", "LANDED STATE", ground, "on ground" if ground else "unknown / airborne")
        return checks

    def report(self):
        airborne = self.values.get("in_air") is True
        checks = self.checks(airborne=self.values.get("armed") is True)
        if not airborne and self.values.get("armed") is True:
            ground = self.values.get("in_air") is False and self.values.get("landed") == "ON_GROUND"
            checks.append(
                {
                    "key": "ground",
                    "label": "LANDED STATE",
                    "status": "ok" if ground else "bad",
                    "value": "on ground" if ground else "unknown / airborne",
                }
            )
        good = all(c["status"] != "bad" for c in checks)
        now = self.clock()
        if not good:
            self.good_since = None
        elif self.good_since is None:
            self.good_since = now
        stable_for = max(0.0, now - self.good_since) if self.good_since is not None else 0.0
        stable = good and stable_for >= STABLE_S
        checks.append(
            {
                "key": "stability",
                "label": "CONTINUOUS READINESS",
                "status": "ok" if stable else "bad",
                "value": f"{min(stable_for, STABLE_S):.1f} / {STABLE_S:g} s",
            }
        )
        reasons = [f"{c['label']}: {c['value']}" for c in checks if c["status"] == "bad"]
        flight_checks = self.flight_checks()
        armed = self.values.get("armed")
        return {
            "bridge_id": self.bridge_id,
            "ts": time.time(),
            "ok": stable,
            "link_connected": self.connected and self.fresh("position"),
            "verdict": "PASSED" if stable else "BLOCKED",
            "checks": checks,
            "reasons": reasons,
            "stable_for_s": round(stable_for, 1),
            "armed": armed,
            "in_air": self.values.get("in_air"),
            "landed": self.values.get("landed"),
            "mode": self.values.get("mode"),
            "alt": (self.values.get("position") or {}).get("alt"),
            "can_arm": stable and armed is False and not airborne,
            "can_disarm": self.fresh("landed")
            and self.fresh("in_air")
            and self.values.get("landed") == "ON_GROUND"
            and self.values.get("in_air") is False,
            "flight_checks": flight_checks,
            "flight_ready": stable and all(c["status"] == "ok" for c in flight_checks),
            "status_text": self.status_text[-5:],
            "limits": {
                "alt_m": TEST_ALT_M,
                "max_alt_m": MAX_ALT_M,
                "radius_m": MAX_RADIUS_M,
                "step_m": STEP_M,
                "speed_m_s": SPEED_M_S,
                "timeout_s": FLIGHT_TIMEOUT_S,
            },
        }

    def flight_checks(self):
        checks = []
        rc = self.values.get("rc") or {}
        ok = self.fresh("rc") and rc.get("available") is True and finite(rc.get("signal")) and rc["signal"] >= 30
        checks.append(
            {
                "key": "rc",
                "label": "RC BACKUP",
                "status": "ok" if ok else "bad",
                "value": "available" if ok else "fresh RC signal ≥30% required",
            }
        )
        params = self.values.get("params") or {}
        for name, (_, rule, expected) in PARAM_RULES.items():
            value = params.get(name)
            ok = self.fresh("params", 30) and finite(value) and rule(value)
            checks.append(
                {
                    "key": name,
                    "label": name,
                    "status": "ok" if ok else "bad",
                    "value": str(value) if ok else f"{value if value is not None else 'unknown'}; need {expected}",
                }
            )
        return checks


def expire_report(report, received_at, now=None):
    """Web-side expiry also covers a dead bridge / disconnected MQTT bus."""
    now = time.monotonic() if now is None else now
    report = dict(report or {})
    if received_at is None or not 0 <= now - received_at <= FRESH_S:
        report.update(
            ok=False,
            can_arm=False,
            can_disarm=False,
            can_move=False,
            link_connected=False,
            flight_ready=False,
            verdict="BLOCKED",
        )
        reason = "BRIDGE TELEMETRY: stale / not received"
        report["reasons"] = [reason, *report.get("reasons", [])]
        report["checks"] = [
            {"key": "bridge", "label": "BRIDGE TELEMETRY", "status": "bad", "value": "stale / not received"}
        ]
    report["report_age_s"] = round(now - received_at, 1) if received_at is not None else None
    return report
