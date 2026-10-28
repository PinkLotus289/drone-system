"""Isolated PX4/MAVSDK integration test, never a real-airframe test.

Run from the repository: PYTHONPATH=src .venv/bin/python scripts/check_real_flight_sitl.py
Requires the project's built PX4 SITL binary. Creates instance 8 and temporary
rootfs; never accepts a drone URL, uses MQTT, or modifies production config.
Physical RC availability is explicitly injected for this virtual aircraft ONLY.
Production RC permission and COM_RC_IN_MODE checks are not bypassed by this tool.
Logs: logs/real-flight-sitl-{run,px4}.log. Do not run alongside another instance 8.
"""

import asyncio
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from drone_core.real_safety import RealSafety
from simulator.real_flight import RealFlight
from simulator.guarded_mavsdk import run_guarded
from simulator.mavsdk_bridge import setup_sitl_params

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "PX4-Autopilot/build/px4_sitl_default"


class Bus:
    def publish(self, topic, payload, **kw):
        pass


class VirtualRcSafety(RealSafety):
    # The virtual aircraft has no physical RC receiver. Inject ONLY these two
    # dependencies in this isolated check; production RealSafety is unchanged.
    def flight_checks(self):
        checks = super().flight_checks()
        for c in checks:
            if c["key"] in {"rc", "COM_RC_IN_MODE"}:
                c.update(status="ok", value="SIMULATED RC dependency")
        return checks


async def main(system):
    await setup_sitl_params(system, "[ISOLATED VIRTUAL AIRFRAME]")
    ints = {"MAV_TYPE": 2, "COM_OBL_RC_ACT": 4, "NAV_DLL_ACT": 3, "COM_DL_LOSS_T": 3, "COM_RC_OVERRIDE": 3}
    floats = {
        "COM_OF_LOSS_T": 1.0,
        "COM_DISARM_LAND": 2.0,
        "MPC_XY_VEL_MAX": 0.8,
        "MPC_TKO_SPEED": 0.5,
        "MPC_LAND_SPEED": 0.3,
    }
    for k, v in ints.items():
        await system.param.set_param_int(k, v)
    for k, v in floats.items():
        await system.param.set_param_float(k, v)
    c = RealFlight(system, Bus(), "virtual_test_8", VirtualRcSafety())

    async def stream(method, key, convert):
        async for value in method():
            c.safety.observe(key, convert(value))

    tasks = [
        asyncio.create_task(x)
        for x in [
            c.collect(),
            c.monitor(),
            stream(
                system.telemetry.position,
                "position",
                lambda p: dict(lat=p.latitude_deg, lon=p.longitude_deg, alt=p.relative_altitude_m),
            ),
            stream(
                system.telemetry.attitude_euler,
                "attitude",
                lambda a: dict(roll=a.roll_deg, pitch=a.pitch_deg, yaw=a.yaw_deg),
            ),
            stream(
                system.telemetry.gps_info,
                "gps",
                lambda g: dict(satellites=g.num_satellites, fix_type=str(g.fix_type).split(".")[-1]),
            ),
            stream(system.telemetry.battery, "battery", lambda b: dict(pct=b.remaining_percent, voltage_v=b.voltage_v)),
        ]
    ]

    async def send(action, cid, **extra):
        await c.command(
            action,
            dict(
                command_id=cid,
                issued_at=time.time(),
                bridge_id=c.safety.bridge_id,
                operator_token="virtual-operator",
                safety_confirmed=True,
                **extra,
            ),
        )

    async def operator():
        while True:
            await send("test_keepalive", "keepalive")
            await asyncio.sleep(0.5)

    tasks.append(asyncio.create_task(operator()))
    try:
        for i in range(80):
            r = c.snapshot()
            if r["can_arm"] and r["flight_ready"]:
                break
            if i % 10 == 0:
                print("WAIT", i / 2, r["reasons"], [x for x in r["flight_checks"] if x["status"] == "bad"], flush=True)
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("Virtual readiness timeout")
        print("VIRTUAL READY; RC availability is an injected simulation dependency", flush=True)
        await send("arm", "arm")
        if c.task:
            await asyncio.wait_for(asyncio.shield(c.task), 8)
        print("ARM RESULT", c.results.get("arm"), flush=True)
        assert c.results["arm"]["status"] == "completed"
        for _ in range(25):
            if c.snapshot()["ok"]:
                break
            await asyncio.sleep(0.5)
        await send("test_takeoff", "takeoff")
        if c.task:
            await asyncio.wait_for(asyncio.shield(c.task), 30)
        print(
            "TAKEOFF RESULT", c.results.get("takeoff"), "PHASE", c.phase, "POS", c.safety.values.get("ned"), flush=True
        )
        assert c.results["takeoff"]["status"] == "completed" and c.phase == "holding"
        for direction in ["forward", "back", "left", "right"]:
            await send("test_step", direction, direction=direction)
            if c.task:
                await asyncio.wait_for(asyncio.shield(c.task), 12)
            print("STEP", direction, c.results.get(direction), "PHASE", c.phase, flush=True)
            assert c.results[direction]["status"] == "completed"
        await send("test_return", "return")
        if c.task:
            await asyncio.wait_for(asyncio.shield(c.task), 12)
        assert c.results["return"]["status"] == "completed"
        await send("land", "land")
        if c.task:
            await asyncio.wait_for(asyncio.shield(c.task), 25)
        print("LAND RESULT", c.results.get("land"), "PHASE", c.phase, flush=True)
        assert c.results["land"]["status"] == "completed"
        print("ISOLATED SITL FLIGHT PASSED", flush=True)
    finally:
        await c.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def run_isolated():
    import socket

    # Refuse occupied ports rather than accidentally attaching to an existing
    # airframe/simulator. This check's connection endpoints are fixed localhost.
    for kind, port in [(socket.SOCK_DGRAM, 14548), (socket.SOCK_DGRAM, 14588), (socket.SOCK_STREAM, 50348)]:
        with socket.socket(socket.AF_INET, kind) as sock:
            sock.bind(("127.0.0.1", port))
    with tempfile.TemporaryDirectory(prefix="skybite-virtual-check-") as td:
        (ROOT / "logs").mkdir(exist_ok=True)
        log = open(ROOT / "logs/real-flight-sitl-px4.log", "w")
        env = dict(os.environ, PX4_SIM_MODEL="sihsim_quadx", PX4_HOME_LAT="43.0747", PX4_HOME_LON="-89.3842")
        px4 = subprocess.Popen(
            [str(BUILD / "bin/px4"), "-i", "8", "-d", td, "-s", "etc/init.d-posix/rcS"],
            cwd=BUILD,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            time.sleep(4)
            if px4.poll() is not None:
                raise RuntimeError("Virtual PX4 did not start; see logs/real-flight-sitl-px4.log")
            asyncio.run(run_guarded("udp://:14548", 50348, main))
        finally:
            if px4.poll() is None:
                os.killpg(px4.pid, signal.SIGTERM)
                try:
                    px4.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    os.killpg(px4.pid, signal.SIGKILL)
                    px4.wait()
            log.close()


if __name__ == "__main__":
    run_isolated()
