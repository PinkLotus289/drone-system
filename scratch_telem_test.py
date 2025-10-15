import asyncio
import json
import random
from drone_core.infra.messaging.mqtt_bus import MqttBus
import sys, os
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))


async def main():
    bus = MqttBus()
    await bus.connect()

    for i in range(5):
        payload = {"lat": 52.0 + i*0.001, "lon": 21.0, "alt": 30 + i}
        await bus.publish("telem/veh_001/pose", payload)
        print("→ Sent pose:", payload)
        await asyncio.sleep(1)

    await bus.disconnect()

asyncio.run(main())
