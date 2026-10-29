import asyncio
from types import SimpleNamespace
import time
from unittest.mock import AsyncMock

from simulator.real_flight import RealFlight
from test_real_safety import ready, feed


class Bus:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, **kwargs):
        self.messages.append((topic, payload))


def controller():
    s, now = ready()
    system = SimpleNamespace(
        action=SimpleNamespace(arm=AsyncMock(), disarm=AsyncMock(), land=AsyncMock()),
        offboard=SimpleNamespace(set_position_ned=AsyncMock(), start=AsyncMock()),
    )
    return RealFlight(system, Bus(), "veh_11", s), now


def payload(c, cid="one", **extra):
    return dict(
        command_id=cid,
        issued_at=time.time(),
        bridge_id=c.safety.bridge_id,
        operator_token="operator-a",
        safety_confirmed=True,
        **extra,
    )


def test_arm_request_rejection_does_not_invent_armed_state():
    async def run():
        c, _ = controller()
        c.system.action.arm.side_effect = RuntimeError("PX4 denied arm")
        await c.command("arm", payload(c))
        await asyncio.sleep(0)
        for _ in range(10):
            await asyncio.sleep(0.001)
            if c.results["one"]["status"] == "failed":
                break
        assert c.safety.values["armed"] is False
        assert c.results["one"]["status"] == "failed"
        await c.close()

    asyncio.run(run())


def test_arm_completion_requires_telemetry_confirmation():
    async def run():
        c, _ = controller()
        await c.command("arm", payload(c))
        await asyncio.sleep(0.01)
        assert c.results["one"]["status"] == "running"
        c.safety.observe("armed", True)
        await asyncio.wait_for(c.task, 1)
        assert c.results["one"]["status"] == "completed"
        assert c.phase == "armed"
        await c.close()

    asyncio.run(run())


def test_land_cancels_motor_test_and_prevents_delayed_disarm():
    async def run():
        c, _ = controller()

        async def armed():
            c.safety.observe("armed", True)

        c.system.action.arm.side_effect = armed
        await c.command("motor_test", payload(c))
        await asyncio.sleep(0.01)
        assert c.phase == "motor_test"
        # LAND is accepted with no lease/bridge stamp/telemetry permission.
        await c.command("land", {"command_id": "emergency"})
        await asyncio.sleep(0.01)
        assert c.results["one"]["status"] == "cancelled"
        assert c.phase == "landing"
        c.system.action.land.assert_awaited()
        c.system.action.disarm.assert_not_awaited()
        c.safety.observe("armed", False)
        await asyncio.wait_for(c.task, 1)
        assert c.results["emergency"]["status"] == "completed"
        assert c.phase == "landed"

    asyncio.run(run())


def test_stale_replayed_wrong_bridge_forced_and_unrestricted_commands_are_rejected():
    async def run():
        c, _ = controller()
        cases = [dict(issued_at=time.time() - 5), dict(bridge_id="old-bridge"), dict(force=True)]
        for i, change in enumerate(cases):
            data = payload(c, str(i))
            data.update(change)
            await c.command("arm", data)
            assert c.results[str(i)]["status"] == "rejected"
        await c.command("goto", payload(c, "unbounded"))
        assert c.results["unbounded"]["status"] == "rejected"
        c.system.action.arm.assert_not_awaited()
        await c.command("arm", payload(c, "duplicate"))
        c.safety.observe("armed", True)
        await c.task
        await c.command("arm", payload(c, "duplicate"))
        assert c.system.action.arm.await_count == 1
        await c.close()

    asyncio.run(run())


def test_busy_operation_and_other_operator_cannot_queue_more_motion():
    async def run():
        c, _ = controller()
        await c.command("arm", payload(c))
        await c.command("motor_test", payload(c, "busy"))
        assert c.results["busy"]["status"] == "rejected"
        data = payload(c, "other")
        data["operator_token"] = "operator-b"
        await c.command("arm", data)
        assert c.results["other"]["status"] == "rejected"
        await c.close()

    asyncio.run(run())


def test_missing_config_blocks_takeoff_and_airborne_disarm_is_rejected():
    async def run():
        c, _ = controller()
        feed(c.safety, armed=True)
        c.safety.observe("params", {})
        await c.command("test_takeoff", payload(c))
        assert c.results["one"]["status"] == "rejected"
        c.system.offboard.start.assert_not_awaited()
        feed(c.safety, armed=True, airborne=True)
        await c.command("disarm", payload(c, "disarm"))
        assert c.results["disarm"]["status"] == "rejected"
        c.system.action.disarm.assert_not_awaited()

    asyncio.run(run())


def test_watchdog_detects_operator_loss_radius_altitude_and_stale_data():
    c, now = controller()
    c.owner = "operator-a"
    c.last_operator = now[0]
    feed(c.safety, armed=True, airborne=True)
    c.origin = dict(c.safety.values["ned"], d=0)
    c.started = now[0]
    c.flight_active = True
    assert c.violation() is None
    c.safety.observe("ned", dict(c.safety.values["ned"], n=c.origin["n"] + 4))
    assert "radius" in c.violation()
    feed(c.safety, armed=True, airborne=True)
    c.safety.observe("ned", dict(c.safety.values["ned"], d=-3))
    assert "altitude" in c.violation()
    feed(c.safety, armed=True, airborne=True)
    now[0] += 4.1
    assert "keepalive" in c.violation()
    c.last_operator = now[0]
    assert "FRESHNESS" in c.violation()


def test_old_landing_confirmation_is_not_presented_as_current_after_link_loss():
    c, _ = controller()
    c.phase, c.reason = "landed", "Landing and disarm confirmed"
    c.safety.observe("connection", False)
    report = c.snapshot()
    assert "unknown" in report["operation_reason"]
    assert report["armed"] is None
    assert report["in_air"] is None


def test_landing_confirmation_keeps_the_watchdog_trigger_visible():
    async def run():
        c, _ = controller()
        await c._land_operation("time-limit", "Test flight time limit reached")
        assert c.phase == "landed"
        assert "time limit" in c.snapshot()["operation_reason"]
        assert "confirmed" in c.snapshot()["operation_reason"]
        await c.close()

    asyncio.run(run())


def test_half_meter_step_uses_takeoff_heading_and_preserves_height():
    async def run():
        c, _ = controller()
        feed(c.safety, armed=True, airborne=True)
        c.safety.clock = time.monotonic
        stamp = time.monotonic()
        c.safety.seen = {k: stamp for k in c.safety.seen}
        c.safety.good_since = stamp - 10
        c.flight_active, c.phase, c.owner = True, "holding", "operator-a"
        c.origin = dict(c.safety.values["ned"], d=0)
        c.target = dict(c.safety.values["ned"])
        c.yaw = 90  # forward must point east
        c.started = c.safety.clock()

        async def setpoint(point):
            c.safety.observe(
                "ned", {"n": point.north_m, "e": point.east_m, "d": point.down_m, "vn": 0, "ve": 0, "vd": 0}
            )

        c.system.offboard.set_position_ned.side_effect = setpoint
        await c.command("test_step", payload(c, direction="forward"))
        await asyncio.wait_for(c.task, 5)
        assert abs(c.target["e"] - (c.origin["e"] + 0.5)) < 1e-6
        assert abs(c.target["n"] - c.origin["n"]) < 1e-6
        assert c.target["d"] == -1
        assert c.phase == "holding"
        await c.close()

    asyncio.run(run())


def test_rc_mode_override_cancels_test_without_reentering_offboard():
    async def run():
        c, now = controller()
        feed(c.safety, armed=True, airborne=True)
        c.flight_active, c.owner = True, "operator-a"
        c.phase = "moving"
        c.origin = dict(c.safety.values["ned"], d=0)
        c.started = c.last_operator = now[0]
        c.offboard_started = now[0] - 4
        c.safety.observe("mode", "POSCTL")
        c.task = asyncio.create_task(asyncio.sleep(30))
        monitor = asyncio.create_task(c.monitor())
        try:
            await asyncio.sleep(0.01)
            assert c.phase == "manual"
            assert c.task.cancelled()
            c.system.offboard.start.assert_not_awaited()
            c.system.action.land.assert_not_awaited()
            feed(c.safety, armed=False, airborne=False)
            await asyncio.sleep(0.25)
            assert c.phase == "landed"
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            await c.close()

    asyncio.run(run())
