#!/usr/bin/env python3
import os
import shutil
import subprocess
import asyncio
import threading
import time
from pathlib import Path
import yaml


def _drain_stdout(proc, instance, ready_event: threading.Event):
    """Читает stdout PX4 в фоне, чтобы пайп никогда не блокировался.
    При первом 'Ready for takeoff' (или mavlink udp port) выставляет ready_event."""
    try:
        for line in proc.stdout:
            stripped = line.rstrip()
            print(f"[PX4-{instance}] {stripped}")
            if not ready_event.is_set() and (
                "Ready for takeoff" in stripped
                or ("mavlink" in stripped and "udp port" in stripped)
            ):
                # Ждём чтобы все MAVLink endpoints успели стартовать (offboard link и т.д.).
                # "Ready for takeoff" печатается после завершения rcS — к этому моменту
                # все mavlink start выполнены. Это самый надёжный маркер.
                if "Ready for takeoff" in stripped:
                    ready_event.set()
    except Exception as e:
        print(f"[PX4-{instance}] stdout reader stopped: {e}")


async def wait_for_px4_ready(proc, instance, ready_event: threading.Event, timeout: float = 40.0):
    start = time.time()
    while not ready_event.is_set():
        if time.time() - start > timeout:
            raise TimeoutError(f"PX4 instance {instance} не дошёл до 'Ready for takeoff' за {timeout}s")
        await asyncio.sleep(0.3)
    print(f"✅ PX4 instance {instance} готов (все mavlink endpoints запущены)")


def make_env(drone):
    env = os.environ.copy()
    env["PX4_SIM_MODEL"] = "sihsim_quadx"
    env["PX4_HOME_LAT"] = "43.0747"
    env["PX4_HOME_LON"] = "-89.3842"
    # PX4_HOME_ALT намеренно не задаём — иначе PX4 интерпретирует TAKEOFF-altitude
    # как AMSL и кидает WARN "Already higher than takeoff altitude".
    # Портами mavlink управляет px4-rc.mavlink на основе $px4_instance (-i N):
    # GCS link: udp 18570+i (без -o, broadcast-only — работает для 1 дрона),
    # offboard link: udp 14580+i -o 127.0.0.1:14540+i (для multi-drone).
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
    ready_events = []
    for d in cfg["drones"]:
        rootfs = build_dir / f"rootfs_{d['id']}"
        # Чистим rootfs перед каждым запуском: иначе `dataman` содержит миссию
        # от предыдущего запуска и PX4 сразу выполняет её → bridge ловит
        # ложное landing detected ещё до первого takeoff.
        if rootfs.exists():
            shutil.rmtree(rootfs)
        os.makedirs(rootfs, exist_ok=True)
        cmd = [
            str(build_dir / "bin/px4"),
            "-i", str(d["id"]),
            "-d", str(rootfs),
            "-s", "etc/init.d-posix/rcS",
        ]
        env = make_env(d)
        print(f"🚁 Запуск PX4 instance {d['id']} → mavlink_out={d['mavlink_out']}")
        p = subprocess.Popen(cmd, cwd=build_dir, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1, universal_newlines=True)
        ready = threading.Event()
        # Запускаем поток-дренер сразу, чтобы stdout пайп не переполнился
        # (иначе PX4 блокируется на write, и offboard mavlink никогда не стартует).
        threading.Thread(
            target=_drain_stdout, args=(p, d["id"], ready), daemon=True
        ).start()
        procs.append((d, p))
        ready_events.append((d, p, ready))
        await asyncio.sleep(1.5)

    # дожидаемся 'Ready for takeoff' каждого инстанса
    await asyncio.gather(*[wait_for_px4_ready(p, d["id"], ev) for d, p, ev in ready_events])

    print(f"✅ Запущено {len(procs)} PX4-инстансов, все активны.")
    return [p for _, p in procs]
