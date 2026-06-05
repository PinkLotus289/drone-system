#!/usr/bin/env python3
"""Diagnostic: запускает 1 PX4 + bridge + orchestrator, шлёт миссию через MQTT,
снимает телеметрию (alt/in_air/mode/progress) и пишет отчёт.

Цель — поймать момент потери высоты у дрона в реальном PX4 SITL, не в моках.

Использование:
    python3 tests/diagnose_real_flight.py [delivery|patrol|isr]
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
import yaml

PROJECT = Path(__file__).resolve().parent.parent
SRC = PROJECT / "src"
BUILD = PROJECT / "PX4-Autopilot/build/px4_sitl_default"
CFG = SRC / "simulator/config.yaml"
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883

G, R, Y, B, M, NC = "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[95m", "\033[0m"

CHILDREN: list[subprocess.Popen] = []


def kill_all_leftovers() -> None:
    for patt in ("bin/px4", "mavsdk_server", "simulator.mavsdk_bridge",
                 "drone_core.workers.telemetry_ingest",
                 "drone_core.workers.orchestrator"):
        subprocess.run(["pkill", "-9", "-f", patt],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def clear_retained() -> None:
    """Чистит только то что в реальности retain'ится — fleet/active."""
    client = mqtt.Client(client_id=f"diag_clear_{os.getpid()}")
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()
    client.publish("fleet/active", b"", qos=1, retain=True)
    time.sleep(0.5)
    client.loop_stop()
    client.disconnect()


def start_px4(drone_cfg: dict) -> subprocess.Popen:
    import shutil
    instance = drone_cfg["instance"]
    rootfs = BUILD / f"rootfs_{instance}"
    # Чистим rootfs как это делает px4_launcher.py: иначе dataman хранит миссию
    # от прошлого запуска и PX4 сразу её исполняет → ложное «прог 8/9» и кривой полёт.
    if rootfs.exists():
        shutil.rmtree(rootfs)
    rootfs.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PX4_SIM_MODEL"] = "sihsim_quadx"
    env["PX4_HOME_LAT"] = str(drone_cfg.get("home_lat", 43.0747))
    env["PX4_HOME_LON"] = str(drone_cfg.get("home_lon", -89.3842))
    cmd = [
        str(BUILD / "bin/px4"),
        "-i", str(instance),
        "-d", str(rootfs),
        "-s", "etc/init.d-posix/rcS",
    ]
    print(f"{Y}[diag]{NC} starting PX4 instance={instance}…")
    p = subprocess.Popen(
        cmd, cwd=str(BUILD),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True, env=env,
    )

    ready = threading.Event()

    def reader():
        for line in iter(p.stdout.readline, ""):
            if not line:
                break
            if "Ready for takeoff" in line:
                ready.set()
            # Тихо: только важные строки
            if any(s in line for s in ("ERROR", "WARN", "Land", "RTL", "fail")):
                print(f"  {M}[PX4-{instance}]{NC} {line.rstrip()}")

    threading.Thread(target=reader, daemon=True).start()
    for _ in range(80):
        if ready.is_set():
            break
        time.sleep(0.5)
    if not ready.is_set():
        raise RuntimeError(f"PX4 {instance} не дошёл до 'Ready for takeoff'")
    print(f"{G}[diag]{NC} PX4 {instance} ready")
    return p


def start_python(name: str, module: str, env_extra: dict | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    print(f"{Y}[diag]{NC} starting {name}…")
    p = subprocess.Popen(
        ["python3", "-m", module],
        cwd=str(SRC),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True, env=env,
    )

    def reader(prefix: str):
        for line in iter(p.stdout.readline, ""):
            if not line:
                break
            ls = line.rstrip()
            # Фильтр шума — показываем только важное
            if any(s in ls for s in ("ERROR", "WARN", "FAIL", "LAND",
                                       "TAKEOFF", "STATE", "MISSION", "RTL",
                                       "armed=False", "fly_through", "actor",
                                       "[MIS]", "airborne", "🛫", "🚀",
                                       "native[", "do_orbit", "HOVER")):
                print(f"  {B}[{prefix}]{NC} {ls}")

    threading.Thread(target=reader, args=(name,), daemon=True).start()
    return p


def build_patrol_payload(veh_id: str) -> dict:
    """Patrol с 3 WPs + orbit-hover вокруг каждой — новая семантика без descent."""
    import math
    base = {"lat": 43.0747, "lon": -89.3842, "alt": 60.0}
    route = [(43.0760, -89.3830), (43.0770, -89.3820), (43.0760, -89.3810)]
    cruise_alt = 60.0
    cruise_speed = 10.0
    loiter_wp = 5.0  # hover 5с через orbit

    # Эмулируем buildPatrolWaypoints с orbit-hover (15м, 3 m/s, 8 точек)
    HOVER_R = 15
    HOVER_SPEED = 3.0
    HOVER_POINTS = 8

    def orbit_around(lat, lon, dur_s):
        m_per_lat = 111111
        m_per_lon = 111111 * math.cos(math.radians(lat))
        circle = []
        for i in range(HOVER_POINTS):
            a = (i / HOVER_POINTS) * 2 * math.pi
            circle.append((lat + HOVER_R * math.cos(a) / m_per_lat,
                           lon + HOVER_R * math.sin(a) / m_per_lon))
        lap_t = (2 * math.pi * HOVER_R) / HOVER_SPEED
        laps = max(1, math.ceil(dur_s / lap_t))
        out = []
        for _ in range(laps):
            for la, lo in circle:
                out.append({"lat": la, "lon": lo, "alt": cruise_alt, "speed_m_s": HOVER_SPEED})
        return out

    wps = []
    for _loop in range(2):
        for lat, lon in route:
            wps.append({"lat": lat, "lon": lon, "alt": cruise_alt, "speed_m_s": cruise_speed})
            wps.extend(orbit_around(lat, lon, loiter_wp))
    # RTH+LAND
    wps += [
        {"lat": base["lat"], "lon": base["lon"], "alt": cruise_alt, "speed_m_s": cruise_speed},
        {"lat": base["lat"], "lon": base["lon"], "alt": 10.0, "speed_m_s": 2.0},
        {"lat": base["lat"], "lon": base["lon"], "alt": 0.0, "speed_m_s": 2.0, "kind": "LAND"},
    ]
    print(f"  [diag] patrol payload: {len(wps)} wps (3 WPs × 2 loops × (1 transit + ~3 orbit) + 3 RTH)")
    return {
        "id": f"ord_diag_patrol_{int(time.time())}",
        "base": base,
        "addr1": {"lat": route[0][0], "lon": route[0][1], "alt": cruise_alt},
        "addr2": {"lat": route[-1][0], "lon": route[-1][1], "alt": cruise_alt},
        "payload_kg": 0.0,
        "priority": "normal",
        "extras": {
            "drone_id": veh_id,
            "mission_type": "patrol",
            "cruise_alt_m": cruise_alt,
            "cruise_speed_mps": cruise_speed,
            "takeoff_alt_m": 15.0,
            "auto_rth": True,
            "waypoints": wps,
            "loop_count": 2,
        },
    }


def build_delivery_payload(veh_id: str) -> dict:
    """Delivery с descent/hover — может выявить проблему с вертикальным спуском."""
    base = {"lat": 43.0747, "lon": -89.3842, "alt": 60.0}
    pickup = {"lat": 43.0760, "lon": -89.3820}
    drop = {"lat": 43.0770, "lon": -89.3810}
    cruise_alt = 60.0
    cruise_speed = 10.0
    wps = [
        {"lat": pickup["lat"], "lon": pickup["lon"], "alt": cruise_alt, "speed_m_s": cruise_speed},
        {"lat": pickup["lat"], "lon": pickup["lon"], "alt": 10.0, "speed_m_s": 2.0, "loiter_s": 3},
        {"lat": pickup["lat"], "lon": pickup["lon"], "alt": cruise_alt, "speed_m_s": 2.0},
        {"lat": drop["lat"], "lon": drop["lon"], "alt": cruise_alt, "speed_m_s": cruise_speed},
        {"lat": drop["lat"], "lon": drop["lon"], "alt": 10.0, "speed_m_s": 2.0, "loiter_s": 3},
        {"lat": drop["lat"], "lon": drop["lon"], "alt": cruise_alt, "speed_m_s": 2.0},
        {"lat": base["lat"], "lon": base["lon"], "alt": cruise_alt, "speed_m_s": cruise_speed},
        {"lat": base["lat"], "lon": base["lon"], "alt": 10.0, "speed_m_s": 2.0},
        {"lat": base["lat"], "lon": base["lon"], "alt": 0.0, "speed_m_s": 2.0, "kind": "LAND"},
    ]
    return {
        "id": f"ord_diag_delivery_{int(time.time())}",
        "base": base,
        "addr1": {"lat": pickup["lat"], "lon": pickup["lon"], "alt": cruise_alt},
        "addr2": {"lat": drop["lat"], "lon": drop["lon"], "alt": cruise_alt},
        "payload_kg": 2.0,
        "priority": "normal",
        "extras": {
            "drone_id": veh_id,
            "mission_type": "delivery",
            "cruise_alt_m": cruise_alt,
            "cruise_speed_mps": cruise_speed,
            "takeoff_alt_m": 15.0,
            "auto_rth": True,
            "waypoints": wps,
        },
    }


class TelemetryWatcher:
    """Подписан на telem/+/pose, mission/+/progress, mission/+/status, fleet/active.
    Логирует alt, mission_progress, FLYING/IDLE — снимок каждые 2 сек."""

    def __init__(self):
        self.alt_history: list[tuple[float, float]] = []  # (ts, alt)
        self.last_progress = (0, 0)
        self.last_status: str | None = None
        self.last_mission_status: str | None = None
        self.events: list[dict] = []
        self.client = mqtt.Client(client_id=f"diag_telem_{os.getpid()}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, *_):
        client.subscribe("telem/+/pose", qos=0)
        client.subscribe("telem/+/velocity", qos=0)
        client.subscribe("mission/+/progress", qos=0)
        client.subscribe("mission/+/status", qos=1)
        client.subscribe("fleet/active", qos=0)

    def _on_message(self, client, userdata, msg):
        try:
            p = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        ts = time.time()
        if msg.topic.endswith("/pose"):
            self.alt_history.append((ts, float(p.get("alt", 0))))
        elif msg.topic.endswith("/progress"):
            self.last_progress = (int(p.get("current", 0)), int(p.get("total", 0)))
        elif msg.topic.endswith("/status"):
            self.last_mission_status = str(p.get("status"))
            self.events.append({"ts": ts, "kind": "mission_status",
                                "status": p.get("status")})
        elif msg.topic == "fleet/active":
            self.last_status = str(p.get("status"))

    def start(self):
        self.client.connect(MQTT_HOST, MQTT_PORT, 60)
        self.client.loop_start()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def snapshot(self) -> str:
        recent = self.alt_history[-1] if self.alt_history else (0, 0)
        cur, tot = self.last_progress
        return (f"alt={recent[1]:.1f}m  prog={cur}/{tot}  "
                f"fleet={self.last_status}  mission={self.last_mission_status}")


def publish_order(payload: dict) -> None:
    client = mqtt.Client(client_id=f"diag_pub_{os.getpid()}")
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()
    client.publish("orders/new", json.dumps(payload), qos=1)
    time.sleep(0.3)
    client.loop_stop()
    client.disconnect()


def main():
    mission_type = sys.argv[1] if len(sys.argv) > 1 else "patrol"
    if mission_type not in ("patrol", "delivery"):
        print(f"unknown mission type: {mission_type}")
        sys.exit(2)

    print(f"\n{B}{'='*72}{NC}")
    print(f"{B} REAL-SIM DIAGNOSTIC · mission={mission_type}{NC}")
    print(f"{B}{'='*72}{NC}\n")

    # 0) Очистка
    kill_all_leftovers()
    time.sleep(1.0)
    clear_retained()

    # 1) PX4 instance 0
    cfg = yaml.safe_load(CFG.read_text())
    drone0 = cfg["drones"][0]
    px4 = start_px4(drone0)
    CHILDREN.append(px4)
    time.sleep(2.0)

    # 2) Bridge для drone 0
    bridge = start_python("bridge-0", "simulator.mavsdk_bridge", {"DRONE_ID": "0"})
    CHILDREN.append(bridge)
    time.sleep(5.0)  # ждём пока MAVSDK подключится и пошлёт fleet/active IDLE

    # 3) Orchestrator
    orch = start_python("orch", "drone_core.workers.orchestrator")
    CHILDREN.append(orch)
    time.sleep(3.0)

    # 4) Telemetry watcher
    watcher = TelemetryWatcher()
    watcher.start()
    time.sleep(1.0)

    # 5) Шлём миссию
    veh = "veh_0"
    if mission_type == "patrol":
        payload = build_patrol_payload(veh)
    else:
        payload = build_delivery_payload(veh)
    print(f"\n{Y}[diag]{NC} dispatching {mission_type} mission with "
          f"{len(payload['extras']['waypoints'])} waypoints to {veh}\n")
    publish_order(payload)

    # 6) Снимки каждые 3 сек — 4 минуты
    start = time.time()
    DURATION = 240.0
    last_alt_anomaly: float | None = None
    min_alt_after_takeoff: float = 99.0
    print(f"\n{B}--- Telemetry stream (every 3s, {int(DURATION)}s total) ---{NC}")
    while time.time() - start < DURATION:
        snap = watcher.snapshot()
        elapsed = int(time.time() - start)
        print(f"  [t+{elapsed:3d}s] {snap}")
        # Аномалия: дрон взлетел (alt>5m), потом снизился < 3m, не на финальной фазе
        if watcher.alt_history:
            cur_alt = watcher.alt_history[-1][1]
            if cur_alt > 5 and elapsed > 10:
                min_alt_after_takeoff = min(min_alt_after_takeoff, cur_alt)
            if cur_alt < 3 and elapsed > 30 and watcher.last_progress[0] < watcher.last_progress[1]:
                if last_alt_anomaly is None:
                    last_alt_anomaly = time.time()
                    print(f"  {R}⚠ ALTITUDE LOSS: alt={cur_alt:.1f}m at t+{elapsed}s, "
                          f"mission progress {watcher.last_progress[0]}/{watcher.last_progress[1]} "
                          f"— дрон НЕ должен быть так низко на этой фазе{NC}")
        if watcher.last_mission_status == "COMPLETED":
            print(f"{G}[diag] mission COMPLETED at t+{elapsed}s — exit{NC}")
            break
        time.sleep(3.0)

    # 7) Финальный отчёт
    print(f"\n{B}{'='*72}{NC}")
    print(f"{B} Summary{NC}")
    print(f"{B}{'='*72}{NC}")
    print(f"  Mission type: {mission_type}")
    print(f"  Final mission status: {watcher.last_mission_status}")
    print(f"  Final progress: {watcher.last_progress}")
    print(f"  Min altitude (after takeoff): {min_alt_after_takeoff:.1f}m")
    if last_alt_anomaly is not None:
        print(f"  {R}!!! Altitude loss detected !!!{NC}")
    # alt timeline summary
    if watcher.alt_history:
        samples = watcher.alt_history[::max(1, len(watcher.alt_history)//12)][:12]
        print(f"  Alt timeline (sec, m): "
              + ", ".join(f"{int(t - watcher.alt_history[0][0])}:{a:.0f}" for t, a in samples))

    # 8) Cleanup
    watcher.stop()
    kill_all_leftovers()


if __name__ == "__main__":
    try:
        main()
    finally:
        kill_all_leftovers()
