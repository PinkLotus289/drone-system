import asyncio
import yaml
from mavsdk import System
from mavsdk.telemetry import FlightMode
import aiomqtt
import logging

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')


class DroneBridge:
    def __init__(self, instance_id: int, port: int, mqtt_client):
        self.id = instance_id
        self.port = port
        self.drone = System()
        self.mqtt = mqtt_client
        self.connected = False

    async def connect(self):
        target_port = 14580 + self.id  # PX4 SITL uses 14580, 14581, etc.
        logging.info(f"[Drone {self.id}] Connecting to UDP port {target_port} ...")
        await self.drone.connect(system_address=f"udp://127.0.0.1:{target_port}")

        async for state in self.drone.core.connection_state():
            if state.is_connected:
                self.connected = True
                logging.info(f"[Drone {self.id}] ✅ Connected to PX4 (sys_id={self.id})")
                break

    async def publish_telemetry(self):
        async for pos in self.drone.telemetry.position():
            data = {
                "id": self.id,
                "lat": pos.latitude_deg,
                "lon": pos.longitude_deg,
                "abs_alt": pos.absolute_altitude_m,
            }
            await self.mqtt.publish(f"drone/{self.id}/telemetry", str(data).encode())

    async def publish_state(self):
        async for state in self.drone.telemetry.flight_mode():
            await self.mqtt.publish(
                f"drone/{self.id}/state",
                state.name.encode()
            )

    async def listen_commands(self):
        topic = f"drone/{self.id}/command"
        await self.mqtt.subscribe(topic)
        logging.info(f"[Drone {self.id}] Listening for commands on {topic}")

        async for message in self.mqtt.messages:
            cmd = message.payload.decode().strip().lower()
            logging.info(f"[Drone {self.id}] 🛰 Command received: {cmd}")
            try:
                if cmd == "takeoff":
                    await self.drone.action.arm()
                    await self.drone.action.takeoff()
                elif cmd == "land":
                    await self.drone.action.land()
                elif cmd == "disarm":
                    await self.drone.action.disarm()
                elif cmd.startswith("goto"):
                    _, lat, lon, alt = cmd.split()
                    await self.drone.action.goto_location(float(lat), float(lon), float(alt), 0)
                else:
                    logging.warning(f"[Drone {self.id}] Unknown command: {cmd}")
            except Exception as e:
                logging.error(f"[Drone {self.id}] Command error: {e}")


async def load_config(path="src/simulator/config.yaml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)


async def main():
    config = await load_config()
    mqtt_cfg = config["mqtt"]
    num_drones = config["simulator"]["num_drones"]

    async with aiomqtt.Client(mqtt_cfg["host"], port=mqtt_cfg["port"]) as mqtt_client:
        tasks = []
        for i in range(num_drones):
            drone = DroneBridge(instance_id=i, port=14580 + i, mqtt_client=mqtt_client)
            await drone.connect()
            tasks += [
                asyncio.create_task(drone.publish_telemetry()),
                asyncio.create_task(drone.publish_state()),
                asyncio.create_task(drone.listen_commands()),
            ]
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("🛑 MAVSDK bridge stopped.")
