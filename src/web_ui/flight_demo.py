"""Session-local walkthrough using the production safety/command controller.

The virtual adapter has no transport, MQTT, profiles, processes or hardware
address. This is a UI/logic exercise, not an aerodynamic or PX4 simulation.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from drone_core.real_safety import RealSafety, expire_report
from simulator.real_flight import ACTIONS, RealFlight

router = APIRouter(prefix="/api/demo/flight", tags=["flight walkthrough"])
DEMO_PARAMS = {
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
Scenario = Literal["healthy", "compass", "battery", "gps", "link_loss"]


class DemoBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemoCommand(DemoBody):
    action: str
    direction: Literal["forward", "back", "left", "right"] | None = None
    safety_confirmed: bool = False


class DemoScenario(DemoBody):
    scenario: Scenario


class VirtualBoard:
    def __init__(self):
        self.n = self.e = self.d = 0.0
        self.target = (0.0, 0.0, 0.0)
        self.velocity = (0.0, 0.0, 0.0)
        self.armed = False
        self.mode = "HOLD"
        self.scenario = "healthy"
        self.link_lost_at = None
        # The controller sees only these in-memory action interfaces.
        self.action = self
        self.offboard = self

    async def _ack(self):
        await asyncio.sleep(0.15)
        if self.scenario == "link_loss":
            raise ConnectionError("DEMO radio link is lost")

    async def arm(self):
        await self._ack()
        self.armed = True

    async def disarm(self):
        await self._ack()
        if self.d < -0.02:
            raise ValueError("DEMO board refuses airborne disarm")
        self.armed = False

    async def land(self):
        await self._ack()
        self.mode = "LAND"
        self.target = (self.n, self.e, 0.0)

    async def start(self):
        await self._ack()
        if not self.armed:
            raise ValueError("DEMO board is not armed")
        self.mode = "OFFBOARD"

    async def set_position_ned(self, point):
        if self.scenario == "link_loss":
            raise ConnectionError("DEMO radio link is lost")
        self.target = (point.north_m, point.east_m, point.down_m)

    def advance(self, dt):
        # Demonstrate a configured onboard loss failsafe even while its
        # telemetry cannot reach the panel. Reconnection confirms landing.
        if self.link_lost_at and time.monotonic() - self.link_lost_at > 1 and self.armed:
            self.mode = "LAND"
            self.target = (self.n, self.e, 0.0)
        before = (self.n, self.e, self.d)
        if self.armed and self.mode in {"OFFBOARD", "LAND"}:
            distance = math.dist(before, self.target)
            ratio = min(1, (0.5 if self.mode == "LAND" else 0.4) * dt / max(distance, 1e-9))
            self.n, self.e, self.d = (p + (q - p) * ratio for p, q in zip(before, self.target))
        if self.mode == "LAND" and self.d >= -0.001:
            self.d, self.armed, self.mode = 0.0, False, "HOLD"
        self.velocity = tuple((q - p) / dt for p, q in zip(before, (self.n, self.e, self.d)))


class DemoBus:
    """Reports stay in this session, never on the application's MQTT bus."""

    def __init__(self):
        self.report = None
        self.received = None

    def publish(self, topic, payload, **kwargs):
        self.report, self.received = payload, time.monotonic()


class DemoSession:
    def __init__(self):
        self.board, self.bus = VirtualBoard(), DemoBus()
        self.controller = RealFlight(self.board, self.bus, "walkthrough", RealSafety())
        self.operator = uuid.uuid4().hex
        self.last_access = time.monotonic()
        self.feed()
        self.controller.safety.status_text.append(
            {"type": "DEMO", "text": "Virtual board connected. Readiness requires 10 seconds.", "ts": time.time()}
        )
        self.monitor = asyncio.create_task(self.controller.monitor())
        self.task = asyncio.create_task(self.run())

    def feed(self):
        b, s = self.board, self.controller.safety
        s.observe("connection", b.scenario != "link_loss")
        if b.scenario == "link_loss":
            return
        airborne = b.d < -0.02
        s.observe("armed", b.armed)
        s.observe("in_air", airborne)
        s.observe("landed", "IN_AIR" if airborne else "ON_GROUND")
        s.observe("mode", b.mode)
        s.observe("position", {"lat": 43.0747 + b.n / 111111, "lon": -89.3842 + b.e / 81111, "alt": -b.d})
        s.observe("ned", dict(n=b.n, e=b.e, d=b.d, vn=b.velocity[0], ve=b.velocity[1], vd=b.velocity[2]))
        s.observe("attitude", {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
        s.observe(
            "gps",
            {"satellites": 3 if b.scenario == "gps" else 17, "fix_type": "FIX_2D" if b.scenario == "gps" else "FIX_3D"},
        )
        s.observe(
            "battery",
            {"pct": 18 if b.scenario == "battery" else 93, "voltage_v": 14.2 if b.scenario == "battery" else 16.4},
        )
        good = b.scenario == "healthy"
        s.observe(
            "health",
            {
                "gyro_ok": True,
                "accel_ok": True,
                "mag_ok": b.scenario != "compass",
                "local_pos_ok": b.scenario != "gps",
                "global_pos_ok": b.scenario != "gps",
                "home_ok": b.scenario != "gps",
                "armable": good and not b.armed,
            },
        )
        s.observe("rc", {"available": True, "signal": 90})
        s.observe("params", dict(DEMO_PARAMS))

    async def run(self):
        try:
            while time.monotonic() - self.last_access < 120 and not self.monitor.done():
                self.board.advance(0.1)
                self.feed()
                await asyncio.sleep(0.1)
        finally:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            await self.controller.close()

    def state(self):
        self.last_access = time.monotonic()
        # Read the current controller state; the monitor timestamp still
        # expires a dead session. Do not mix a new scenario with an old report.
        report = expire_report(self.controller.snapshot(), self.bus.received)
        reported = self.controller.safety.values.get("ned") or {"n": 0, "e": 0}
        report.update(
            demo=True,
            scenario=self.board.scenario,
            veh="walkthrough",
            position={"north_m": round(reported["n"], 2), "east_m": round(reported["e"], 2)},
            report_age_s=round(time.monotonic() - self.bus.received, 1) if self.bus.received else None,
        )
        values = self.controller.safety.values
        report["telemetry"] = (
            {
                "lat": (values.get("position") or {}).get("lat"),
                "lon": (values.get("position") or {}).get("lon"),
                "alt": report.get("alt"),
                "battery_pct": (values.get("battery") or {}).get("pct"),
                "sat_count": (values.get("gps") or {}).get("satellites"),
                "status": "FLYING" if report.get("in_air") else "IDLE",
            }
            if report.get("link_connected")
            else {}
        )
        return report

    def scenario(self, name):
        self.last_access = time.monotonic()
        self.board.scenario = name
        self.board.link_lost_at = time.monotonic() if name == "link_loss" else None
        self.controller.safety.status_text.append(
            {"type": "DEMO", "text": f"Training scenario: {name}", "ts": time.time()}
        )
        self.controller.safety.status_text = self.controller.safety.status_text[-5:]
        self.feed()

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


class DemoSessions:
    def __init__(self):
        self.sessions = {}

    async def start(self, sid):
        self.sessions = {key: value for key, value in self.sessions.items() if not value.task.done()}
        if sid in self.sessions:
            return self.sessions[sid]
        if len(self.sessions) >= 16:
            raise HTTPException(status_code=429, detail="Demo is busy; try again shortly")
        session = DemoSession()
        self.sessions[sid] = session
        await asyncio.sleep(0)  # allow the first genuine controller report
        return session

    def get(self, sid):
        session = self.sessions.get(sid)
        if not session or session.task.done():
            raise HTTPException(status_code=404, detail="Demo session expired. Start or reset the demo.")
        return session

    async def stop(self, sid):
        session = self.sessions.pop(sid, None)
        if session:
            await session.close()


demos = DemoSessions()


def sid_of(request):
    sid = (getattr(request.state, "auth", {}) or {}).get("sid")
    if not sid:
        raise HTTPException(status_code=401, detail="unauthorized")
    return sid


@router.post("/start")
async def demo_start(request: Request):
    return (await demos.start(sid_of(request))).state()


@router.get("/state")
async def demo_state(request: Request):
    return JSONResponse(demos.get(sid_of(request)).state(), headers={"Cache-Control": "no-store"})


@router.post("/command")
async def demo_command(body: DemoCommand, request: Request):
    if body.action not in ACTIONS:
        raise HTTPException(status_code=400, detail="Unsupported demo action")
    session = demos.get(sid_of(request))
    session.last_access = time.monotonic()
    cid = uuid.uuid4().hex
    await session.controller.command(
        body.action,
        {
            "command_id": cid,
            "issued_at": time.time(),
            "bridge_id": session.controller.safety.bridge_id,
            "operator_token": session.operator,
            "direction": body.direction,
            "safety_confirmed": body.safety_confirmed,
        },
    )
    return JSONResponse({"ok": True, "demo": True, "status": "queued", "command_id": cid}, status_code=202)


@router.post("/scenario")
async def demo_scenario(body: DemoScenario, request: Request):
    session = demos.get(sid_of(request))
    session.scenario(body.scenario)
    return {"ok": True, "demo": True}


@router.delete("")
async def demo_stop(request: Request):
    await demos.stop(sid_of(request))
    return {"ok": True, "demo": True}
