from types import SimpleNamespace
from unittest.mock import Mock

from drone_core.infra.messaging.mqtt_bus import MqttBus


def test_wildcard_handler_does_not_receive_other_vehicle_or_telemetry():
    bus = MqttBus("mqtt://127.0.0.1:1883", client_id="routing-unit-test")
    commands, telemetry, exact = Mock(), Mock(), Mock()
    bus.subscribe("cmd/veh_11/#", commands)
    bus.subscribe("telem/+/pose", telemetry)
    bus.subscribe("cmd/veh_11/land", exact)
    for topic in ("telem/veh_11/land", "cmd/veh_12/arm", "telem/veh_11/pose"):
        bus._on_message(None, None, SimpleNamespace(topic=topic, payload=b"{}", qos=1, retain=False))
    commands.assert_not_called()
    exact.assert_not_called()
    assert telemetry.call_count == 1
    bus._on_message(None, None, SimpleNamespace(topic="cmd/veh_11/land", payload=b"{}", qos=1, retain=False))
    assert commands.call_count == 1
    assert exact.call_count == 1
    bus._async_loop.close()
