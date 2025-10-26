#!/usr/bin/env python3
import asyncio
import subprocess
import os
import time
from pathlib import Path
from mavsdk import System

ROOT = Path(__file__).resolve().parent
PX4_DIR = ROOT / "PX4-Autopilot"
BUILD_DIR = PX4_DIR / "build/px4_sitl_default"

# Конфигурация дронов
DRONES = [
    {"id": 0, "udp_in": 14580, "udp_out": 14550},
    {"id": 1, "udp_in": 14581, "udp_out": 14551},
]

def make_env(drone):
    env = os.environ.copy()
    env["PX4_SIM_MODEL"] = "none"
    env["PX4_HOME_LAT"] = "43.0747"
    env["PX4_HOME_LON"] = "-89.3842"
    env["PX4_HOME_ALT"] = "270"
    env["MAV_BROADCAST"] = "1"
    env["MAV_0_BROADCAST"] = "1"
    env["MAV_1_BROADCAST"] = "1"
    env["MAVLINK_UDP_REMOTE_PORT"] = str(drone["udp_out"])
    return env

async def wait_for_px4_ready(proc, instance):
    """Читает вывод PX4 и ждёт, пока MAVLink поднимется"""
    start = time.time()
    while True:
        line = proc.stdout.readline()
        if not line:
            await asyncio.sleep(0.2)
            continue
        print(f"[PX4-{instance}] {line.strip()}")
        if "mavlink" in line and "udp port" in line:
            print(f"✅ PX4 instance {instance} MAVLink активен!")
            break
        if time.time() - start > 20:
            raise TimeoutError(f"PX4 instance {instance} не запустил MAVLink вовремя")

async def connect_mavsdk(instance, port):
    """Подключаемся к PX4 через MAVSDK"""
    drone = System()
    addr = f"udp://:{port}"
    print(f"[MAVSDK-{instance}] ⏳ Подключаемся к PX4 через {addr} ...")
    await drone.connect(system_address=addr)

    async for state in drone.core.connection_state():
        if state.is_connected:
            print(f"[MAVSDK-{instance}] ✅ Подключено к PX4!")
            break

    async for pos in drone.telemetry.position():
        print(f"[MAVSDK-{instance}] 🌍 Позиция: {pos.latitude_deg:.6f}, {pos.longitude_deg:.6f}, высота {pos.relative_altitude_m:.1f} м")
        break
    return drone

async def main():
    px4_procs = []

    # 1. Запуск двух PX4
    for drone in DRONES:
        rootfs = BUILD_DIR / f"rootfs_{drone['id']}"
        os.makedirs(rootfs, exist_ok=True)
        cmd = [
            str(BUILD_DIR / "bin/px4"),
            "-i", str(drone["id"]),
            "-d", str(rootfs),
            "-s", "etc/init.d-posix/rcS",
        ]
        env = make_env(drone)
        print(f"🚁 Запуск PX4 instance {drone['id']} → UDP {drone['udp_in']} (слушает), → {drone['udp_out']} (шлёт heartbeat)")
        p = subprocess.Popen(cmd, cwd=BUILD_DIR, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        px4_procs.append((drone, p))
        await asyncio.sleep(1.5)

    # 2. Проверяем, что оба PX4 активны
    await asyncio.gather(*[wait_for_px4_ready(p, d["id"]) for d, p in px4_procs])

    # 3. Подключаемся через MAVSDK к каждому
    drones = await asyncio.gather(*[
        connect_mavsdk(d["id"], d["udp_out"]) for d, _ in px4_procs
    ])

    # 4. Живём 10 секунд и завершаем
    print("⏳ Ждём 10 секунд (дроны активны)...")
    await asyncio.sleep(10)

    print("🛑 Останавливаем PX4...")
    for _, p in px4_procs:
        p.terminate()
        p.wait()

if __name__ == "__main__":
    asyncio.run(main())
