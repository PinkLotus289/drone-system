import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from web_ui import main
from web_ui import auth
from web_ui.control import ControlAuthority
from test_real_safety import ready


@pytest.fixture
def api(monkeypatch):
    s, _ = ready()
    report = s.report()
    report.update(phase="idle", operation_busy=False)
    main.app.state.active_drones = {"veh_11": {"safety": report, "safety_received": time.monotonic()}}
    connected = threading.Event()
    connected.set()
    bus = SimpleNamespace(_connected=connected, publish=Mock())
    monkeypatch.setattr(main, "bus", bus)
    monkeypatch.setattr(main, "control", ControlAuthority())
    monkeypatch.setattr(main.real_supervisor, "is_connected", lambda name: name == "11")
    token, sid = auth.make_session_token("test operator", main.settings.SESSION_SECRET, 1)
    # No lifespan/startup: these tests must not connect to a broker or airframe.
    client = TestClient(main.app)
    client.cookies.set(auth.COOKIE_NAME, token)
    yield client, sid, bus
    client.close()


def test_real_arm_requires_control_and_returns_queued_not_armed(api):
    client, sid, bus = api
    denied = client.post("/api/drone/veh_11/command", json={"action": "arm"})
    assert denied.status_code == 403
    main.control.acquire("system", "test operator", sid)
    result = client.post("/api/drone/veh_11/command", json={"action": "arm", "safety_confirmed": True})
    assert result.status_code == 202
    assert result.json()["status"] == "queued"
    topic, payload = bus.publish.call_args.args
    assert topic == "cmd/veh_11/arm"
    assert payload["bridge_id"]
    assert payload["operator_token"] == sid
    assert "armed" not in result.json()


def test_authenticated_land_bypasses_lease_and_readiness(api):
    client, _, bus = api
    main.app.state.active_drones = {}
    result = client.post("/api/drone/veh_11/command", json={"action": "land"})
    assert result.status_code == 202
    assert bus.publish.call_args.args[0] == "cmd/veh_11/land"


def test_land_still_requires_authentication(api):
    client, _, bus = api
    client.cookies.clear()
    assert client.post("/api/drone/veh_11/command", json={"action": "land"}).status_code == 401
    bus.publish.assert_not_called()


def test_stale_bridge_blocks_arm_and_readiness_endpoint(api):
    client, sid, bus = api
    main.control.acquire("system", "test operator", sid)
    main.app.state.active_drones["veh_11"]["safety_received"] -= 5
    assert client.post("/api/drone/veh_11/command", json={"action": "arm"}).status_code == 409
    report = client.get("/api/real/preflight/11").json()
    assert not report["can_arm"]
    assert not report["ok"]
    bus.publish.assert_not_called()


def test_no_overrides_or_unbounded_goto_on_real_airframe(api):
    client, sid, bus = api
    main.control.acquire("system", "test operator", sid)
    for body in ({"action": "arm", "force": True}, {"action": "goto", "lat": 44, "lon": -87, "alt": 30}):
        assert client.post("/api/drone/veh_11/command", json=body).status_code == 400
    bus.publish.assert_not_called()


def test_mqtt_outage_is_not_reported_as_queued(api):
    client, _, bus = api
    bus._connected.clear()
    assert client.post("/api/drone/veh_11/command", json={"action": "land"}).status_code == 503
    bus.publish.assert_not_called()


def test_disconnect_blocked_while_armed_or_state_unknown(api, monkeypatch):
    client, _, _ = api
    disconnect = Mock()
    monkeypatch.setattr(main.real_supervisor, "disconnect", disconnect)
    main.app.state.active_drones["veh_11"]["safety"]["armed"] = True
    assert client.post("/api/real/disconnect/11").status_code == 409
    main.app.state.active_drones = {}
    assert client.post("/api/real/disconnect/11").status_code == 409
    disconnect.assert_not_called()
