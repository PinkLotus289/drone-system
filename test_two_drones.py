#!/usr/bin/env python3
import asyncio
import subprocess
import os
from pathlib import Path
from mavsdk import System

ROOT = Path(__file__).resolve().parent
PX4_DIR = ROOT / "PX4-Autopilot"
BUILD_DIR = PX4_DIR / "build/px4_sitl_default"

# Конфигурация двух дронов
DRONES = [
    {"id": 0, "udp_in": 14580, "udp_out": 14550, "home": (43.0747, -89.3842, 270.0)},
    {"id": 1, "udp_in": 14581, "udp_out": 14551, "home": (43.0749, -89.3845, 270.0)},
]


def make_env(drone):
    env = os.environ.copy()
    env["PX4_SIM_MODEL"] = "sihsim_quadx"  # ✅ настоящая модель PX4 SITL
    env["PX4_HOME_LAT"] = str(drone["home"][0])
    env["PX4_HOME_LON"] = str(drone["home"][1])
    env["PX4_HOME_ALT"] = str(drone["home"][2])
    env["MAV_BROADCAST"] = "1"
    env["MAV_0_BROADCAST"] = "1"
    env["MAV_1_BROADCAST"] = "1"
    env["MAVLINK_UDP_REMOTE_PORT"] = str(drone["udp_out"])
    return env


async def wait_for_px4_ready(proc, instance):
    """Ждём пока PX4 запустит MAVLink"""
    while True:
        line = proc.stdout.readline()
        if not line:
            await asyncio.sleep(0.1)
            continue
        print(f"[PX4-{instance}] {line.strip()}")
        if "mavlink" in line and "udp port" in line:
            print(f"✅ PX4 instance {instance} MAVLink активен!")
            break


async def connect_mavsdk(instance, port):
    """Подключаемся к PX4 через MAVSDK"""
    drone = System()
    await drone.connect(system_address=f"udp://:{port}")

    async for state in drone.core.connection_state():
        if state.is_connected:
            print(f"[MAVSDK-{instance}] ✅ Подключено к PX4!")
            break
    return drone


async def prepare_and_fly(drone: System, instance: int):
    """Ждём готовности PX4 и выполняем короткий полёт"""
    print(f"[Drone-{instance}] ⏳ Ожидание готовности системы...")
    async for health in drone.telemetry.health():
        if health.is_global_position_ok and health.is_home_position_ok:
            print(f"[Drone-{instance}] ✅ GPS и EKF готовы.")
            break
        await asyncio.sleep(1.0)

    print(f"[Drone-{instance}] 🛫 ARM и взлёт...")
    await drone.action.arm()
    await asyncio.sleep(1)
    await drone.action.takeoff()
    print(f"[Drone-{instance}] 🚀 Взлёт выполнен")

    # Лог позиции
    async for pos in drone.telemetry.position():
        print(f"[Drone-{instance}] 🌍 Позиция: {pos.latitude_deg:.6f}, {pos.longitude_deg:.6f}, {pos.relative_altitude_m:.1f} м")
        break

    # Полёт вперёд
    print(f"[Drone-{instance}] ➡️ Полёт вперёд на 10 метров...")
    await drone.action.goto_location(pos.latitude_deg, pos.longitude_deg + 0.0001, pos.relative_altitude_m + 5.0, 0)
    await asyncio.sleep(10)

    print(f"[Drone-{instance}] 🏁 Возврат домой...")
    await drone.action.return_to_launch()
    await asyncio.sleep(10)
    print(f"[Drone-{instance}] ✅ Полёт завершён.")


async def main():
    px4_procs = []

    # 1️⃣ Запускаем PX4-инстансы
    for d in DRONES:
        rootfs = BUILD_DIR / f"rootfs_{d['id']}"
        os.makedirs(rootfs, exist_ok=True)
        cmd = [
            str(BUILD_DIR / "bin/px4"),
            "-i", str(d["id"]),
            "-d", str(rootfs),
            "-s", "etc/init.d-posix/rcS",
        ]
        p = subprocess.Popen(
            cmd, cwd=BUILD_DIR, env=make_env(d),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        px4_procs.append((d, p))
        await asyncio.sleep(1.5)

    # 2️⃣ Ждём, пока MAVLink поднимется
    await asyncio.gather(*[wait_for_px4_ready(p, d["id"]) for d, p in px4_procs])

    # 3️⃣ Подключаем MAVSDK
    drones = await asyncio.gather(*[
        connect_mavsdk(d["id"], d["udp_out"]) for d, _ in px4_procs
    ])

    # 4️⃣ Настраиваем и запускаем полёт
    await asyncio.gather(
        prepare_and_fly(drones[0], 0),
        prepare_and_fly(drones[1], 1),
    )

    # 5️⃣ Завершаем симуляции
    for _, p in px4_procs:
        p.terminate()
        p.wait()
    print("✅ Тест завершён.")


if __name__ == "__main__":
    asyncio.run(main())
