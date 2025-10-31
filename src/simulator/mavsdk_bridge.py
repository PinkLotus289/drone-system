#!/usr/bin/env python3
import asyncio
from mavsdk import System
import yaml
from pathlib import Path
import time
import sys

# гарантируем доступ к drone_core
sys.path.append(str(Path(__file__).resolve().parents[1]))

from drone_core.domain.models import Vehicle, VehicleStatus, LLA
from drone_core.infra.repositories.fleet_mem import FleetMem
from drone_core.infra.messaging.mqtt_bus import MqttBus
from drone_core.config.settings import Settings

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


async def connect_to_px4(drone_id: int, port: int, name: str, bus: MqttBus, fleet_repo: FleetMem, home_lat: float, home_lon: float, home_alt: float):
    drone = System()
    addr = f"udp://:{port}"
    print(f"[MAVSDK-{drone_id}] ⏳ Подключаемся к PX4 через {addr} ...")

    try:
        await drone.connect(system_address=addr)
        # ждём подключения
        async for state in drone.core.connection_state():
            if state.is_connected:
                print(f"[MAVSDK-{drone_id}] ✅ Подключено к PX4!")

                # создаём Vehicle объект
                vehicle = Vehicle(
                    id=str(drone_id),
                    name=name,
                    max_payload_kg=5.0,
                    home=LLA(lat=home_lat, lon=home_lon, alt=home_alt),
                    status=VehicleStatus.IDLE,
                    last_seen_ts=time.time(),
                    max_range_km=5.0,
                    speed_mps=10.0
                )

                # добавляем в FleetRepo
                await fleet_repo.add(vehicle)

                # публикуем в MQTT, чтобы визуал увидел дрон
                bus.publish(
                    "fleet/active",
                    {
                        "id": str(drone_id),
                        "name": name,
                        "status": "IDLE",
                        "lat": home_lat,
                        "lon": home_lon,
                        "alt": home_alt
                    },
                    qos=1
                )
                print(f"[MAVSDK-{drone_id}] 🚀 Зарегистрирован как активный дрон.")
                return drone

        print(f"[MAVSDK-{drone_id}] ❌ PX4 не ответил в течение ожидания")
    except Exception as e:
        print(f"[MAVSDK-{drone_id}] ⚠️ Ошибка подключения: {e}")
    return None


async def main():
    cfg_path = CONFIG_PATH
    cfg = yaml.safe_load(cfg_path.read_text())
    drones = cfg["drones"]
    sim_home = cfg["simulator"]["home"]

    settings = Settings()
    bus = MqttBus(settings.MQTT_URL, client_id="mavsdk-bridge")
    bus.start()

    fleet_repo = FleetMem()

    tasks = []
    for d in drones:
        tasks.append(
            connect_to_px4(
                d["id"],
                d["mavlink_out"],
                d["name"],
                bus,
                fleet_repo,
                sim_home["lat"],
                sim_home["lon"],
                sim_home["alt"]
            )
        )

    await asyncio.gather(*tasks)

    # держим bridge живым
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("🛑 Останавливаем MAVSDK Bridge...")
        bus.stop()


if __name__ == "__main__":
    asyncio.run(main())
