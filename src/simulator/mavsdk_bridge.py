#!/usr/bin/env python3
import asyncio
import json
import logging
import time
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from drone_core.config.settings import Settings
from drone_core.infra.messaging.mqtt_bus import MqttBus
import yaml
from mavsdk import System

log = logging.getLogger("mavsdk-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


async def connect_system(connection_url: str) -> System:
    sys = System()
    await sys.connect(system_address=connection_url)
    async for state in sys.core.connection_state():
        if state.is_connected:
            return sys
    return sys


async def handle_command(msg, sys: System, name: str):
    """Обработка команд, приходящих по MQTT (от orchestrator)."""
    log.info(f"[{name}] ⚡ MQTT команда получена: {msg.topic}")
    try:
        if isinstance(msg.payload, (bytes, bytearray)):
            payload = json.loads(msg.payload.decode("utf-8"))
        elif isinstance(msg.payload, str):
            payload = json.loads(msg.payload)
        else:
            payload = msg.payload
    except Exception as e:
        log.error(f"[{name}] Ошибка парсинга команды: {e}")
        return

    topic = msg.topic
    cmd = topic.split("/")[-1]
    log.info(f"[{name}] 📡 Получена команда: {cmd} → {payload}")

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
            log.info(f"[{name}] 🧭 Переход к точке ({lat}, {lon}, {alt})")
        elif cmd == "land":
            await sys.action.land()
            log.info(f"[{name}] 🛬 Посадка")
        else:
            log.warning(f"[{name}] ⚠️ Неизвестная команда: {cmd}")
    except Exception as e:
        log.error(f"[{name}] ❌ Ошибка выполнения команды {cmd}: {e}")


async def run_for_drone(bus: MqttBus, instance_id: str, connection_url: str,
                        home_lat: float, home_lon: float, home_alt: float):
    name = f"veh_{instance_id}"

    # === ⬇️ ПЕРИОДИЧЕСКОЕ ОПОВЕЩЕНИЕ О ДРОНЕ (fleet/active) ===
    async def announce_loop():
        while True:
            payload = {
                "id": name,
                "name": name,
                "status": "IDLE",
                "lat": float(home_lat or 43.0747),
                "lon": float(home_lon or -89.3842),
                "alt": float(home_alt or 0.0),
                "soc": 100.0
            }
            log.info(f"[{name}] 📡 Публикую fleet/active → {json.dumps(payload)}")
            bus.publish("fleet/active", payload, qos=1)
            await asyncio.sleep(10)  # 🔁 каждые 10 секунд

    # ============================================================

    # Первичная публикация (чтобы UI сразу увидел)
    bus.publish(
        "fleet/active",
        {
            "id": name,
            "name": name,
            "status": "IDLE",
            "lat": float(home_lat or 43.0747),
            "lon": float(home_lon or -89.3842),
            "alt": float(home_alt or 0.0),
            "soc": 100.0
        },
        qos=1
    )

    log.info(f"[{name}] Connecting MAVSDK -> {connection_url}")
    sys = await connect_system(connection_url)
    log.info(f"[{name}] ✅ MAVSDK connected")

    # Ждём готовности GPS и home position
    async for h in sys.telemetry.health():
        if h.is_global_position_ok and h.is_home_position_ok:
            break

    # Подписка на MQTT команды
    loop = asyncio.get_running_loop()
    bus.subscribe(
        f"cmd/{name}/#",
        lambda m: asyncio.run_coroutine_threadsafe(handle_command(m, sys, name), loop)
    )

    # === Публикация телеметрии ===
    async def pump_position():
        async for pos in sys.telemetry.position():
            bus.publish(
                f"telem/{name}/pose",
                {
                    "lat": pos.latitude_deg,
                    "lon": pos.longitude_deg,
                    "abs_alt_m": pos.absolute_altitude_m,
                    "rel_alt_m": pos.relative_altitude_m,
                    "ts": time.time(),
                },
                qos=0,
            )

    async def pump_status():
        async for arming in sys.telemetry.armed():
            bus.publish(
                f"telem/{name}/status",
                {"armed": arming, "ts": time.time()},
                qos=0,
            )

    await asyncio.gather(
        announce_loop(),  # 🔁 теперь дрон будет виден постоянно
        pump_position(),
        pump_status()
    )


async def main_async():
    cfg_path = Path(__file__).resolve().parent / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    drones = cfg["drones"]
    sim_home = cfg["simulator"]["home"]
    home_lat, home_lon, home_alt = sim_home["lat"], sim_home["lon"], sim_home.get("alt", 0.0)

    settings = Settings()
    bus = MqttBus(settings.MQTT_URL, client_id="mavsdk-bridge")
    bus.start()

    tasks = []
    for d in drones:
        instance_id = str(d["id"])
        out_port = d["mavlink_out"]
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
