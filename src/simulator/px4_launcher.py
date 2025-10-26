#!/usr/bin/env python3
import os
import subprocess
import asyncio
import time
from pathlib import Path
import yaml


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
        if time.time() - start > 25:
            raise TimeoutError(f"PX4 instance {instance} не запустил MAVLink вовремя")


def make_env(drone):
    env = os.environ.copy()
    env["PX4_SIM_MODEL"] = "none"
    env["PX4_HOME_LAT"] = "43.0747"
    env["PX4_HOME_LON"] = "-89.3842"
    env["PX4_HOME_ALT"] = "270"
    env["MAV_BROADCAST"] = "1"
    env["MAV_0_BROADCAST"] = "1"
    env["MAV_1_BROADCAST"] = "1"
    env["MAVLINK_UDP_REMOTE_PORT"] = str(drone["mavlink_out"])
    env["MAVLINK_UDP_PORT"] = str(drone["udp_port"])
    return env


async def start_px4_instances(cfg: dict):
    """Асинхронно запускает все PX4-инстансы и ждёт готовности MAVLink."""
    px4_dir = (Path(__file__).resolve().parents[2] / cfg["drones"][0]["px4_path"]).resolve()
    build_dir = px4_dir / "build/px4_sitl_default"

    # проверим сборку
    print("[PX4] Проверяем сборку PX4 SITL...")
    subprocess.run(["make", "px4_sitl", "CMAKE_CXX_STANDARD=17"], cwd=px4_dir, check=True)
    print("[PX4] ✅ PX4 собран.")

    procs = []
    for d in cfg["drones"]:
        rootfs = build_dir / f"rootfs_{d['id']}"
        os.makedirs(rootfs, exist_ok=True)
        cmd = [
            str(build_dir / "bin/px4"),
            "-i", str(d["id"]),
            "-d", str(rootfs),
            "-s", "etc/init.d-posix/rcS",
        ]
        env = make_env(d)
        print(f"🚁 Запуск PX4 instance {d['id']} → UDP {d['udp_port']} (слушает), → {d['mavlink_out']} (шлёт heartbeat)")
        p = subprocess.Popen(cmd, cwd=build_dir, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, universal_newlines=True)
        procs.append((d, p))
        await asyncio.sleep(1.5)

    # дожидаемся, пока все MAVLink активируются
    await asyncio.gather(*[wait_for_px4_ready(p, d["id"]) for d, p in procs])

    print(f"✅ Запущено {len(procs)} PX4-инстансов, все активны.")
    return [p for _, p in procs]
