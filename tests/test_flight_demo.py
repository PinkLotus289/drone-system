import asyncio
import time
from unittest.mock import Mock

import httpx

from web_ui import main, auth, flight_demo


def run_test(check, monkeypatch):
    async def run():
        manager = flight_demo.DemoSessions()
        monkeypatch.setattr(flight_demo, "demos", manager)
        publish = Mock(side_effect=AssertionError("Demo must not publish to hardware MQTT"))
        connect = Mock(side_effect=AssertionError("Demo must not start a real bridge"))
        monkeypatch.setattr(main.bus, "publish", publish)
        monkeypatch.setattr(main.real_supervisor, "connect", connect)
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo.test") as client:
            token, sid = auth.make_session_token("demo tester", main.settings.SESSION_SECRET, 1)
            client.cookies.set(auth.COOKIE_NAME, token)
            try:
                await check(client, manager, sid)
                publish.assert_not_called()
                connect.assert_not_called()
            finally:
                for key in list(manager.sessions):
                    await manager.stop(key)

    asyncio.run(run())


def test_demo_requires_auth_and_continuous_readiness(monkeypatch):
    async def check(client, manager, sid):
        started = await client.post("/api/demo/flight/start")
        assert started.status_code == 200
        assert started.json()["demo"] is True
        assert not started.json()["can_arm"]
        result = await client.post("/api/demo/flight/command", json={"action": "arm", "safety_confirmed": True})
        await asyncio.sleep(0.25)  # wait for the controller's report tick
        state = (await client.get("/api/demo/flight/state")).json()
        assert next(c for c in state["commands"] if c["id"] == result.json()["command_id"])["status"] == "rejected"
        assert manager.get(sid).board.armed is False
        client.cookies.clear()
        assert (await client.post("/api/demo/flight/start")).status_code == 401

    run_test(check, monkeypatch)


def test_demo_sessions_are_isolated_and_cannot_accept_hardware_addresses(monkeypatch):
    async def check(client, manager, sid):
        first = (await client.post("/api/demo/flight/start")).json()
        for body in (
            {"action": "arm", "target": "veh_11"},
            {"action": "arm", "connection_url": "udp://:14540"},
            {"action": "arm", "force": True},
        ):
            assert (await client.post("/api/demo/flight/command", json=body)).status_code == 422
        assert (await client.post("/api/demo/flight/command", json={"action": "goto"})).status_code == 400
        other, _ = auth.make_session_token("another tester", main.settings.SESSION_SECRET, 1)
        client.cookies.set(auth.COOKIE_NAME, other)
        assert (await client.get("/api/demo/flight/state")).status_code == 404
        second = (await client.post("/api/demo/flight/start")).json()
        assert first["bridge_id"] != second["bridge_id"]
        assert len(manager.sessions) == 2
        await client.post("/api/demo/flight/scenario", json={"scenario": "compass"})
        assert manager.get(sid).board.scenario == "healthy"

    run_test(check, monkeypatch)


def test_fault_blocks_arm_then_recovery_restarts_stability_timer(monkeypatch):
    async def check(client, manager, sid):
        await client.post("/api/demo/flight/start")
        session = manager.get(sid)
        session.controller.safety.good_since = time.monotonic() - 11
        assert session.controller.snapshot()["can_arm"]
        await client.post("/api/demo/flight/scenario", json={"scenario": "compass"})
        assert not session.controller.snapshot()["can_arm"]
        assert any("MAG" in r for r in session.controller.snapshot()["reasons"])
        await client.post("/api/demo/flight/scenario", json={"scenario": "healthy"})
        assert not session.controller.snapshot()["can_arm"]
        await client.post("/api/demo/flight/scenario", json={"scenario": "battery"})
        assert any("BATTERY" in r for r in session.controller.snapshot()["reasons"])
        await client.post("/api/demo/flight/scenario", json={"scenario": "gps"})
        assert any("GPS" in r for r in session.controller.snapshot()["reasons"])
        assert (await client.post("/api/demo/flight/scenario", json={"scenario": "unsupported"})).status_code == 422

    run_test(check, monkeypatch)


def test_demo_arm_motor_state_land_and_reset_use_controller_acknowledgements(monkeypatch):
    async def check(client, manager, sid):
        await client.post("/api/demo/flight/start")
        session = manager.get(sid)
        old_bridge = session.controller.safety.bridge_id
        session.controller.safety.good_since = time.monotonic() - 11
        armed = await client.post("/api/demo/flight/command", json={"action": "arm", "safety_confirmed": True})
        assert armed.json()["status"] == "queued"
        assert session.board.armed is False
        await asyncio.wait_for(session.controller.task, 2)
        assert session.board.armed is True
        assert session.controller.results[armed.json()["command_id"]]["status"] == "completed"
        await client.post("/api/demo/flight/command", json={"action": "land"})
        await asyncio.wait_for(session.controller.task, 2)
        assert session.board.armed is False
        assert session.controller.phase == "landed"
        await client.delete("/api/demo/flight")
        assert session.task.done()
        reset = (await client.post("/api/demo/flight/start")).json()
        assert reset["bridge_id"] != old_bridge
        assert reset["armed"] is False
        assert not reset["can_arm"]

    run_test(check, monkeypatch)


def test_link_loss_blocks_state_and_virtual_onboard_failsafe_lands(monkeypatch):
    async def check(client, manager, sid):
        await client.post("/api/demo/flight/start")
        session = manager.get(sid)
        session.board.armed, session.board.d, session.board.mode = True, -0.5, "OFFBOARD"
        session.scenario("link_loss")
        assert not session.controller.snapshot()["link_connected"]
        session.board.link_lost_at = time.monotonic() - 2
        session.board.advance(1.1)
        assert session.board.armed is False
        assert session.board.d == 0
        session.scenario("healthy")
        assert session.controller.snapshot()["armed"] is False

    run_test(check, monkeypatch)


def test_abandoned_demo_releases_its_background_tasks(monkeypatch):
    async def check(client, manager, sid):
        await client.post("/api/demo/flight/start")
        session = manager.get(sid)
        session.last_access = time.monotonic() - 121
        await asyncio.wait_for(session.task, 1)
        assert session.monitor.done()
        assert (await client.get("/api/demo/flight/state")).status_code == 404

    run_test(check, monkeypatch)
