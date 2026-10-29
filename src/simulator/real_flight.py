"""Bounded, interruptible real-multicopter test operations (no SITL changes)."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import math
import time

from mavsdk.offboard import PositionNedYaw

from drone_core.real_safety import (
    RealSafety,
    PARAM_RULES,
    FRESH_S,
    TEST_ALT_M,
    MAX_ALT_M,
    MAX_RADIUS_M,
    STEP_M,
    SPEED_M_S,
    FLIGHT_TIMEOUT_S,
    OPERATOR_TIMEOUT_S,
    finite,
)

log = logging.getLogger("real-flight")
ACTIONS = {
    "arm",
    "disarm",
    "motor_test",
    "test_hop",
    "test_takeoff",
    "test_step",
    "test_return",
    "hold",
    "land",
    "test_keepalive",
}


class RealFlight:
    def __init__(self, system, bus, name, safety=None):
        self.system, self.bus, self.name = system, bus, name
        self.safety = safety or RealSafety()
        self.task = None
        self.phase = "idle"
        self.reason = ""
        self.owner = None
        self.last_operator = 0.0
        self.started = None
        self.origin = None
        self.target = None
        self.yaw = None
        self.flight_active = False
        self.offboard_started = None
        self.results = OrderedDict()
        self._land_lock = asyncio.Lock()
        self._last_land = -1e9

    def snapshot(self):
        result = self.safety.report()
        busy = bool(self.task and not self.task.done())
        result.update(
            phase=self.phase,
            operation_busy=busy,
            operation_reason=self.reason,
            flight_active=self.flight_active,
            commands=list(self.results.values())[-8:],
        )
        if not result["link_connected"]:
            result["operation_reason"] = "Telemetry unavailable — current landing and disarm state are unknown."
        result["can_arm"] = result["can_arm"] and not busy and self.phase not in {"landing", "fault"}
        result["can_move"] = (
            self.flight_active
            and not busy
            and self.phase == "holding"
            and result["ok"]
            and self.safety.values.get("mode") == "OFFBOARD"
        )
        result["flight_remaining_s"] = (
            max(0, round(FLIGHT_TIMEOUT_S - (self.safety.clock() - self.started), 1))
            if self.started is not None
            else None
        )
        return result

    def result(self, command_id, action, status, reason=""):
        if not command_id:
            return
        self.results[command_id] = {
            "id": command_id,
            "action": action,
            "status": status,
            "reason": reason,
            "ts": time.time(),
        }
        while len(self.results) > 100:
            self.results.popitem(last=False)
        log.info("[%s] [COMMAND] %s %s %s %s", self.name, command_id, action, status, reason)

    async def command(self, action, payload):
        cid = payload.get("command_id")
        # Emergency LAND is never blocked by readiness, ownership or stale data.
        if action == "land":
            if cid and cid in self.results:
                return
            self.result(cid, action, "accepted")
            self.phase = "landing"  # latch synchronously before any await
            self._replace(self._land_operation(cid, "operator requested LAND"))
            return
        if cid and cid in self.results:
            return
        issued = payload.get("issued_at")
        valid = finite(issued) and -1 <= time.time() - issued <= FRESH_S
        valid = valid and payload.get("bridge_id") == self.safety.bridge_id
        if not valid or payload.get("force"):
            self.result(cid, action, "rejected", "Expired command, wrong bridge, or prohibited override")
            return
        operator = payload.get("operator_token")
        if not operator or (self.owner and operator != self.owner):
            self.result(cid, action, "rejected", "Another operator owns this test; LAND remains available")
            return
        if action == "test_keepalive":
            if operator == self.owner:
                self.last_operator = self.safety.clock()
            return
        try:
            self._validate(action, payload)
        except ValueError as exc:
            self.result(cid, action, "rejected", str(exc))
            return
        self.last_operator = self.safety.clock()
        self.owner = operator
        self.result(cid, action, "accepted")
        self.task = asyncio.create_task(self._operation(cid, action, payload))

    def _validate(self, action, payload):
        report = self.snapshot()
        if action not in ACTIONS:
            raise ValueError("Use bounded real-flight test controls; unrestricted commands are disabled")
        if report["operation_busy"] or self.phase in {"landing", "fault", "manual"}:
            raise ValueError("Operation in progress or landing unconfirmed; LAND remains available")
        if action == "disarm":
            if not report["can_disarm"]:
                raise ValueError("Disarm requires fresh, confirmed ON_GROUND and in_air=False")
            return
        if not report["ok"]:
            raise ValueError("; ".join(report["reasons"]))
        if action in {"arm", "motor_test"}:
            if not report["can_disarm"]:
                raise ValueError("Ground state required")
            if action == "arm" and not report["can_arm"]:
                raise ValueError("Already armed or arming unavailable")
            if action == "motor_test" and report["armed"] is not False:
                raise ValueError("Bench test must start disarmed, with propellers removed")
            if payload.get("safety_confirmed") is not True:
                raise ValueError("Explicit safety confirmation required")
        elif action in {"test_takeoff", "test_hop"}:
            if not report["flight_ready"]:
                raise ValueError(
                    "; ".join(f"{c['label']}: {c['value']}" for c in report["flight_checks"] if c["status"] != "ok")
                )
            if not report["can_disarm"] or report["armed"] is not True or self.flight_active:
                raise ValueError("Arm first; test takeoff requires confirmed armed ground state")
            if payload.get("safety_confirmed") is not True:
                raise ValueError("Confirm propellers, clear outdoor area and RC backup before flight")
        elif action in {"test_step", "test_return", "hold"}:
            if not report["can_move"]:
                raise ValueError("A supervised test hover is required")
            if action == "test_step" and payload.get("direction") not in {"forward", "back", "left", "right"}:
                raise ValueError("Invalid direction")

    def _replace(self, coroutine):
        previous = self.task
        if previous and not previous.done():
            previous.cancel()

        async def run():
            if previous:
                await asyncio.gather(previous, return_exceptions=True)
            await coroutine

        self.task = asyncio.create_task(run())

    async def _rpc(self, awaitable):
        return await asyncio.wait_for(awaitable, 2.0)

    async def _until(self, predicate, timeout=5):
        deadline = self.safety.clock() + timeout
        while self.safety.clock() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.1)
        raise TimeoutError("Telemetry did not confirm the requested state")

    async def _operation(self, cid, action, payload):
        self.result(cid, action, "running")
        try:
            if action == "arm":
                self.phase = "arming"
                await self._rpc(self.system.action.arm())
                await self._until(lambda: self.safety.values.get("armed") is True and self.safety.fresh("armed"))
                self.phase = "armed"
            elif action == "disarm":
                await self._rpc(self.system.action.disarm())
                await self._until(lambda: self.safety.values.get("armed") is False and self.safety.fresh("armed"))
                self.phase, self.owner = "idle", None
            elif action == "motor_test":
                self.phase = "motor_test"
                await self._rpc(self.system.action.arm())
                await self._until(lambda: self.safety.values.get("armed") is True)
                await asyncio.sleep(2.0)
                if not self.safety.report()["can_disarm"]:
                    raise RuntimeError("Ground state lost during motor test")
                await self._rpc(self.system.action.disarm())
                await self._until(lambda: self.safety.values.get("armed") is False)
                self.phase, self.owner = "idle", None
            elif action in {"test_takeoff", "test_hop"}:
                self.origin = dict(self.safety.values["ned"])
                self.target = dict(self.origin)
                self.yaw = self.safety.values["attitude"]["yaw"]
                self.started = self.safety.clock()
                self.flight_active = True
                self.phase = "taking_off"
                # Prime at the measured position, never (0,0,0). PX4 needs a
                # setpoint stream for >1 s before entering Offboard.
                for _ in range(12):
                    await self._setpoint(self.target)
                    await asyncio.sleep(0.1)
                await self._rpc(self.system.offboard.start())
                self.offboard_started = self.safety.clock()
                await self._until(lambda: self.safety.values.get("mode") == "OFFBOARD", timeout=4)
                await self._move_to(dict(self.origin, d=self.origin["d"] - TEST_ALT_M))
                await self._until(lambda: self.safety.values.get("in_air") is True)
                self.phase = "holding"
                if action == "test_hop":
                    await asyncio.sleep(2)
                    self.phase = "landing"
                    await self._land_operation(None, "test hop completed")
            else:
                self.phase = "moving"
                if action == "hold":
                    target = dict(self.safety.values["ned"])
                elif action == "test_return":
                    target = dict(self.origin, d=self.origin["d"] - TEST_ALT_M)
                else:
                    yaw = math.radians(self.yaw)
                    forward, right = {
                        "forward": (STEP_M, 0),
                        "back": (-STEP_M, 0),
                        "left": (0, -STEP_M),
                        "right": (0, STEP_M),
                    }[payload["direction"]]
                    target = dict(
                        self.target,
                        n=self.target["n"] + forward * math.cos(yaw) - right * math.sin(yaw),
                        e=self.target["e"] + forward * math.sin(yaw) + right * math.cos(yaw),
                    )
                if math.hypot(target["n"] - self.origin["n"], target["e"] - self.origin["e"]) > MAX_RADIUS_M - 0.5:
                    raise ValueError("Target exceeds test radius; return to start or LAND")
                await self._move_to(target)
                self.phase = "holding"
            self.result(cid, action, "completed")
        except asyncio.CancelledError:
            self.result(cid, action, "cancelled", "Interrupted by LAND / watchdog")
            raise
        except Exception as exc:
            self.result(cid, action, "failed", str(exc))
            self.phase = "landing"
            await self._land_operation(None, f"operation failed: {exc}")

    async def _setpoint(self, point):
        await self._rpc(
            self.system.offboard.set_position_ned(PositionNedYaw(point["n"], point["e"], point["d"], self.yaw))
        )

    async def _move_to(self, goal):
        start = dict(self.target)
        distance = math.sqrt(sum((goal[k] - start[k]) ** 2 for k in ("n", "e", "d")))
        steps = max(1, math.ceil(distance / (SPEED_M_S * 0.1)))
        for i in range(1, steps + 1):
            self.target = {k: start[k] + (goal[k] - start[k]) * i / steps for k in ("n", "e", "d")}
            await self._setpoint(self.target)
            await asyncio.sleep(0.1)
        settled_since = None
        deadline = self.safety.clock() + 8
        while self.safety.clock() < deadline:
            pos = self.safety.values.get("ned", {})
            error = math.sqrt(sum((pos.get(k, 1e6) - goal[k]) ** 2 for k in ("n", "e", "d")))
            speed = math.sqrt(sum(pos.get(k, 1e6) ** 2 for k in ("vn", "ve", "vd")))
            if error <= 0.25 and speed <= 0.4:
                if settled_since is None:
                    settled_since = self.safety.clock()
                if self.safety.clock() - settled_since >= 0.5:
                    return
            else:
                settled_since = None
            await asyncio.sleep(0.1)
        raise TimeoutError("Target position / stopped motion not confirmed")

    async def _send_land(self):
        # MAVSDK stops resending setpoints when it observes mode exit. Do not
        # call offboard.stop() after LAND: that API requests HOLD itself.
        try:
            await self._rpc(self.system.action.land())
            self.reason = "LAND accepted; waiting for confirmed landing and auto-disarm"
        except Exception as exc:
            self.reason = f"LAND unconfirmed: {exc}; retrying — use RC backup if link is lost"
            log.error("[%s] %s", self.name, self.reason)
        self._last_land = self.safety.clock()

    async def _land_operation(self, cid, reason):
        self.phase, self.reason = "landing", reason
        log.warning("[%s] [LAND] %s", self.name, reason)
        async with self._land_lock:
            await self._send_land()
            deadline = self.safety.clock() + 20
            while self.safety.clock() < deadline:
                if self._landed():
                    self._finish_landing()
                    self.result(cid, "land", "completed")
                    return
                await asyncio.sleep(0.2)
                if self.safety.clock() - self._last_land >= 2:
                    await self._send_land()
            self.phase = "fault"
            self.reason = "Landing not confirmed — LAND retries continue; check drone / RC backup"
            self.result(cid, "land", "failed", self.reason)

    def _landed(self):
        return (
            self.safety.fresh("landed")
            and self.safety.fresh("armed")
            and self.safety.fresh("in_air")
            and self.safety.values.get("landed") == "ON_GROUND"
            and self.safety.values.get("in_air") is False
            and self.safety.values.get("armed") is False
        )

    def _finish_landing(self):
        self.phase, self.owner, self.reason = "landed", None, "Landing and disarm confirmed"
        self.flight_active = False
        self.started = None
        self.offboard_started = None

    def violation(self):
        if not self.owner and not self.flight_active:
            return None
        now = self.safety.clock()
        if now - self.last_operator > OPERATOR_TIMEOUT_S:
            return "Operator keepalive lost"
        report = self.safety.report()
        checks = self.safety.checks(airborne=True)
        bad = [c for c in checks if c["status"] == "bad"]
        if bad:
            return "; ".join(f"{c['label']}: {c['value']}" for c in bad)
        if self.flight_active:
            if self.safety.values.get("armed") is not True:
                return "Armed state lost during test flight"
            if now - self.started > FLIGHT_TIMEOUT_S:
                return "Test flight time limit reached"
            if any(c["status"] != "ok" for c in report["flight_checks"]):
                return "RC backup or verified failsafe configuration lost"
            p = self.safety.values["ned"]
            height = self.origin["d"] - p["d"]
            if height > MAX_ALT_M or height < -0.5:
                return "Test altitude limit exceeded / local origin changed"
            if math.hypot(p["n"] - self.origin["n"], p["e"] - self.origin["e"]) > MAX_RADIUS_M:
                return "Test radius exceeded / local origin changed"
            if math.sqrt(sum(p[k] ** 2 for k in ("vn", "ve", "vd"))) > 1.2:
                return "Excessive test flight speed"
            if max(abs(self.safety.values["attitude"][k]) for k in ("roll", "pitch")) > 25:
                return "Excessive tilt"
        return None

    async def monitor(self):
        while True:
            if self.phase == "manual" and self._landed():
                self._finish_landing()
            if self.phase in {"landing", "fault"}:
                if self._landed():
                    self._finish_landing()
                elif (not self.task or self.task.done()) and self.safety.clock() - self._last_land >= 2:
                    await self._send_land()
            elif (
                self.offboard_started is not None
                and self.safety.clock() - self.offboard_started > 3
                and self.safety.values.get("mode") != "OFFBOARD"
            ):
                # Pilot/failsafe mode change has priority. Cancel our motion;
                # never restart Offboard or override that mode automatically.
                if self.task and not self.task.done():
                    self.task.cancel()
                    await asyncio.gather(self.task, return_exceptions=True)
                self.phase, self.reason = "manual", "PX4 exited Offboard — test stopped; pilot / autopilot has control"
                self.owner, self.flight_active, self.offboard_started = None, False, None
            else:
                reason = self.violation()
                if reason:
                    self.phase = "landing"
                    self._replace(self._land_operation(None, reason))
            self.bus.publish(f"telem/{self.name}/safety", self.snapshot(), qos=0)
            await asyncio.sleep(0.2)

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.owner or self.flight_active:
            await self._send_land()

    async def collect(self):
        """Health/armed/landed subscriptions are change-driven in MAVSDK.

        Keep one live subscription, without repeated subscribe/cancel cycles.
        Independent live position/GPS/battery and connection checks expire on
        lost input. A closed state subscription revokes its permission.
        """
        s = self.safety

        async def sample(key, stream, convert):
            try:
                async for value in stream():
                    s.observe(key, convert(value))
            finally:
                s.stream_failed(key)

        async def connection():
            try:
                async for value in self.system.core.connection_state():
                    s.observe("connection", value.is_connected)
            finally:
                s.observe("connection", False)

        async def ned():
            async for value in self.system.telemetry.position_velocity_ned():
                p, v = value.position, value.velocity
                s.observe(
                    "ned",
                    {
                        "n": p.north_m,
                        "e": p.east_m,
                        "d": p.down_m,
                        "vn": v.north_m_s,
                        "ve": v.east_m_s,
                        "vd": v.down_m_s,
                    },
                )

        async def parameters():
            while True:
                params = {}
                for name, (kind, _, _) in PARAM_RULES.items():
                    try:
                        getter = getattr(self.system.param, "get_param_" + kind)
                        params[name] = await asyncio.wait_for(getter(name), 1)
                    except Exception:
                        params[name] = None
                s.observe("params", params)
                await asyncio.sleep(10)

        async def text():
            async for msg in self.system.telemetry.status_text():
                s.status_text.append({"type": str(msg.type), "text": msg.text, "ts": time.time()})
                s.status_text = s.status_text[-5:]
                log.info("[%s] [PX4] %s %s", self.name, msg.type, msg.text)

        def health(h):
            return {
                "gyro_ok": h.is_gyrometer_calibration_ok,
                "accel_ok": h.is_accelerometer_calibration_ok,
                "mag_ok": h.is_magnetometer_calibration_ok,
                "local_pos_ok": h.is_local_position_ok,
                "global_pos_ok": h.is_global_position_ok,
                "home_ok": h.is_home_position_ok,
                "armable": h.is_armable,
            }

        # Rate requests do not change saved PX4 configuration. An unsupported
        # request is diagnostic only; missing live data still blocks permission.
        for name in ("position", "position_velocity_ned", "gps_info", "battery", "attitude_euler"):
            try:
                await asyncio.wait_for(
                    getattr(self.system.telemetry, "set_rate_" + name)(
                        5 if name in {"position", "position_velocity_ned", "attitude_euler"} else 2
                    ),
                    1,
                )
            except Exception as exc:
                log.warning("[%s] stream rate %s: %s", self.name, name, exc)
        tasks = [
            asyncio.create_task(c)
            for c in (
                connection(),
                ned(),
                parameters(),
                text(),
                sample("health", self.system.telemetry.health, health),
                sample("armed", self.system.telemetry.armed, bool),
                sample("in_air", self.system.telemetry.in_air, bool),
                sample("landed", self.system.telemetry.landed_state, lambda v: str(v).split(".")[-1]),
                sample("mode", self.system.telemetry.flight_mode, lambda v: str(v).split(".")[-1]),
                sample(
                    "rc",
                    self.system.telemetry.rc_status,
                    lambda v: {"available": v.is_available, "signal": v.signal_strength_percent},
                ),
            )
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
