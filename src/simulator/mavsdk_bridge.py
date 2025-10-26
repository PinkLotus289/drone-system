#!/usr/bin/env python3
import asyncio
from mavsdk import System
import yaml
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


async def connect_to_px4(drone_id: int, port: int):
    drone = System()
    addr = f"udp://:{port}"
    print(f"[MAVSDK-{drone_id}] ⏳ Подключаемся к PX4 через {addr} ...")

    try:
        await drone.connect(system_address=addr)
        # Ожидаем появления соединения
        for i in range(60):
            async for state in drone.core.connection_state():
                if state.is_connected:
                    print(f"[MAVSDK-{drone_id}] ✅ Подключено к PX4!")
                    return drone
            await asyncio.sleep(1)
        print(f"[MAVSDK-{drone_id}] ❌ PX4 не ответил за 60 секунд")
    except Exception as e:
        print(f"[MAVSDK-{drone_id}] ⚠️ Ошибка подключения: {e}")
    return None


async def main():
    cfg_path = Path(__file__).resolve().parent / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    drones = cfg["drones"]

    tasks = []
    for d in drones:
        port = d["mavlink_out"]
        tasks.append(connect_to_px4(d["id"], port))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
