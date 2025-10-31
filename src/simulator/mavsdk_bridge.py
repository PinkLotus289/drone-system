#!/usr/bin/env python3
# src/simulator/mavsdk_bridge.py
import asyncio
import json
import logging
import time
from pathlib import Path
import sys

# гарантируем доступ к src/
sys.path.append(str(Path(__file__).resolve().parents[1]))

from drone_core.config.settings import Settings
from drone_core.infra.messaging.mqtt_bus import MqttBus
from drone_core.infra.messaging.topics import TelemetryTopics  # только для ALL-шаблона, публикация руками
import yaml
from mavsdk import System
from mavsdk import telemetry as mtelem

log = logging.getLogger("mavsdk-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


async def connect_system(connection_url: str) -> System:
    sys = System()
    await sys.connect(system_address=connection_url)
    async for state in sys.core.connection_state():
        if state.is_connected:
            return sys
    # на всякий
    return sys


async def run_for_drone(bus: MqttBus, instance_id: str, connection_url: str, home_lat: float, home_lon: float, home_alt: float):
    name = f"veh_{instance_id}"

    # Объявим дрона как активного (чтобы UI увидел до телеметрии)
    bus.publish(
        "fleet/active",
        {
            "id": instance_id,
            "name": name,
            "status": "IDLE",
            "lat": home_lat,
            "lon": home_lon,
            "alt": home_alt
        },
        qos=1
    )

    # Подключаемся к MAVSDK
    log.info(f"[{name}] Connecting MAVSDK -> {connection_url}")
    sys = await connect_system(connection_url)
    log.info(f"[{name}] MAVSDK connected")

    # Ждём готовности (health)
    async for h in sys.telemetry.health():
        if h.is_global_position_ok and h.is_home_position_ok:
            break

    # Подписка на позицию и статус — публикуем в MQTT
    async def pump_position():
        async for pos in sys.telemetry.position():
            payload = {
                "lat": pos.latitude_deg,
                "lon": pos.longitude_deg,
                "abs_alt_m": pos.absolute_altitude_m,
                "rel_alt_m": pos.relative_altitude_m,
                "ts": time.time()
            }
            # ПУБЛИКУЕМ сюда — это читает telemetry_ingest по шаблону telemetry/+/+
            bus.publish(f"telem/{instance_id}/pose", payload, qos=0)

    async def pump_status():
        async for arming in sys.telemetry.armed():
            bus.publish(f"telem/{instance_id}/status", {"armed": arming, "ts": time.time()}, qos=0)

    await asyncio.gather(
        pump_position(),
        pump_status()
    )


async def main_async():
    # читаем конфиг
    cfg_path = Path(__file__).resolve().parent / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    drones = cfg["drones"]
    sim_home = cfg["simulator"]["home"]
    home_lat, home_lon, home_alt = sim_home["lat"], sim_home["lon"], sim_home.get("alt", 0.0)

    settings = Settings()
    bus = MqttBus(settings.MQTT_URL, client_id="mavsdk-bridge")
    bus.start()

    # Стартуем пайплайны для всех дронов
    tasks = []
    for d in drones:
        instance_id = str(d["id"])
        out_port = d["mavlink_out"]  # PX4 → OUT порт, на него и слушает MAVSDK (“udp://:PORT”)
        connection_url = f"udp://:{out_port}"
        tasks.append(asyncio.create_task(
            run_for_drone(bus, instance_id, connection_url, home_lat, home_lon, home_alt)
        ))

    try:
        await asyncio.gather(*tasks)
    finally:
        bus.stop()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
