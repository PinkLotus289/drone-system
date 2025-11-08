#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import time
from pathlib import Path
import sys
from typing import Dict, Any
import yaml

# --- путь к проекту ---
sys.path.append(str(Path(__file__).resolve().parents[1]))

from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan
from drone_core.config.settings import Settings
from drone_core.infra.messaging.mqtt_bus import MqttBus

# --- логирование ---
log = logging.getLogger("mavsdk-bridge")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

# =====================================================
#  Подключение к PX4
# =====================================================
async def connect_system(connection_url: str) -> System:
    sys = System()
    log.info(f"🔌 Подключаюсь к PX4 через {connection_url} ...")
    await sys.connect(system_address=connection_url)

    async for state in sys.core.connection_state():
        if state.is_connected:
            log.info(f"✅ MAVSDK connected to {connection_url}")
            return sys

    raise RuntimeError(f"❌ Не удалось подключиться к PX4: {connection_url}")


# =====================================================
#  Обработка MQTT команд
# =====================================================
async def handle_command(msg, sys: System, name: str):
    topic = msg.topic
    cmd = topic.split("/")[-1]
    log.info(f"[{name}] ⚡ MQTT команда получена: topic={topic}")

    # --- парсим payload ---
    try:
        if isinstance(msg.payload, (bytes, bytearray)):
            payload = json.loads(msg.payload.decode("utf-8"))
        elif isinstance(msg.payload, str):
            payload = json.loads(msg.payload)
        else:
            payload = msg.payload or {}
    except Exception as e:
        log.error(f"[{name}] Ошибка парсинга payload команды: {e}")
        return

    log.info(f"[{name}] 📡 cmd={cmd}, payload={payload}")

    # --- выполнение команд ---
    try:
        if cmd == "arm":
            await sys.action.arm()
            log.info(f"[{name}] ✅ Armed")

        elif cmd == "takeoff":
            await sys.action.takeoff()
            log.info(f"[{name}] ✈️ Взлёт")

        elif cmd == "goto":
            lat, lon, alt = payload["lat"], payload["lon"], payload["alt"]
            await sys.action.goto_location(lat, lon, alt, 0)
            log.info(f"[{name}] 🧭 GOTO → ({lat}, {lon}, {alt})")

        elif cmd == "land":
            await sys.action.land()
            log.info(f"[{name}] 🛬 Посадка")

        elif cmd in ("rtl", "return_to_launch"):
            await sys.action.return_to_launch()
            log.info(f"[{name}] 🏠 RTL")

        elif cmd == "mission.upload":
            waypoints = payload.get("waypoints", [])
            if not waypoints:
                log.warning(f"[{name}] ⚠️ Пустая миссия в mission.upload")
                return

            mission_items = []
            for wp in waypoints:
                pos = wp.get("pos", wp)
                lat, lon, alt = pos["lat"], pos["lon"], pos["alt"]

                # ✅ новая сигнатура для MAVSDK ≥2.x
                mission_items.append(
                    MissionItem(
                        lat, lon, alt,                # координаты
                        5.0,                          # speed_m_s
                        True,                         # is_fly_through
                        0.0, 0.0,                     # gimbal_pitch/yaw
                        0.0, 0.0,                     # loiter_time_s, camera_photo_interval_s
                        5.0, 0.0,                     # acceptance_radius_m, yaw_deg
                        0.0,                          # camera_photo_distance_m
                        MissionItem.VehicleAction.NONE
                    )
                )

            plan = MissionPlan(mission_items)
            await sys.mission.upload_mission(plan)
            log.info(f"[{name}] 📦 Миссия загружена ({len(mission_items)} точек)")

        elif cmd == "mission.start":
            await sys.mission.start_mission()
            log.info(f"[{name}] 🚀 Миссия стартовала")

        else:
            log.warning(f"[{name}] ⚠️ Неизвестная команда: {cmd}")

    except Exception as e:
        log.error(f"[{name}] ❌ Ошибка при выполнении {cmd}: {e}")


# =====================================================
#  Логика отдельного дрона
# =====================================================
async def run_for_drone(
    instance_id: str,
    connection_url: str,
    home_lat: float,
    home_lon: float,
    home_alt: float,
):
    name = f"veh_{instance_id}"
    settings = Settings()

    # отдельный MQTT клиент для каждого дрона
    bus = MqttBus(settings.MQTT_URL, client_id=f"mavsdk-{name}-{os.getpid()}")
    bus.start()

    sys = await connect_system(connection_url)

    # ждём GPS
    async for h in sys.telemetry.health():
        if h.is_global_position_ok and h.is_home_position_ok:
            break

    # первая публикация
    bus.publish("fleet/active", {
        "id": name,
        "name": name,
        "status": "IDLE",
        "lat": home_lat,
        "lon": home_lon,
        "alt": home_alt,
        "soc": 100.0,
    }, qos=1)
    log.info(f"[{name}] 👋 Объявился во fleet/active")

    # подписка на команды
    loop = asyncio.get_running_loop()
    bus.subscribe(
        f"cmd/{name}/#",
        lambda m: asyncio.run_coroutine_threadsafe(handle_command(m, sys, name), loop),
        qos=1,
    )
    log.info(f"[{name}] 🔔 Подписан на cmd/{name}/#")

    # --- телеметрия позиции ---
    async def publish_position():
        try:
            async for pos in sys.telemetry.position():
                bus.publish(f"telem/{name}/pose", {
                    "lat": pos.latitude_deg,
                    "lon": pos.longitude_deg,
                    "alt": pos.relative_altitude_m,
                    "ts": time.time(),
                }, qos=0)
                await asyncio.sleep(5.0)
        except Exception as e:
            log.error(f"[{name}] ❌ Ошибка телеметрии позиции: {e}")

    # --- статус FLYING / IDLE ---
    async def monitor_in_air():
        last_status = None
        try:
            async for in_air in sys.telemetry.in_air():
                pos = await sys.telemetry.position().__anext__()
                if pos.relative_altitude_m < 1.0:
                    in_air = False
                status = "FLYING" if in_air else "IDLE"
                if status != last_status:
                    bus.publish("fleet/active", {
                        "id": name,
                        "name": name,
                        "status": status,
                        "lat": pos.latitude_deg,
                        "lon": pos.longitude_deg,
                        "alt": pos.relative_altitude_m,
                        "soc": 100.0,
                    }, qos=0)
                    last_status = status
                await asyncio.sleep(1.0)
        except Exception as e:
            log.error(f"[{name}] ❌ Ошибка статуса in_air: {e}")

    try:
        await asyncio.gather(publish_position(), monitor_in_air())
    finally:
        bus.stop()


# =====================================================
#  Главная точка входа
# =====================================================
async def main_async():
    cfg_path = Path(__file__).resolve().parent / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())

    drones = cfg["drones"]
    sim_home = cfg["simulator"]["home"]
    home_lat, home_lon, home_alt = (
        sim_home["lat"],
        sim_home["lon"],
        sim_home.get("alt", 0.0),
    )

    tasks = []
    for d in drones:
        instance_id = str(d["id"])
        out_port = d["mavlink_out"]
        connection_url = f"udp://:{out_port}"
        tasks.append(asyncio.create_task(
            run_for_drone(instance_id, connection_url, home_lat, home_lon, home_alt)
        ))

    await asyncio.gather(*tasks)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
