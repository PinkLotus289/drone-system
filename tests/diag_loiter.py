#!/usr/bin/env python3
"""Фокус-диагностика: проверяем гипотезу «настоящий PX4 loiter теряет высоту».

Сценарий:
  takeoff -> лететь к точке на cruise_alt -> ЗАВИСНУТЬ на точке HOLD_S сек
  (через mission loiter, hold_s>0, is_fly_through=False) -> RTL+LAND.

Снимаем alt каждую секунду; в окне зависания смотрим, держит ли дрон высоту.

Использование: python3 tests/diag_loiter.py
"""
from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml

# Переиспользуем инфраструктуру запуска из соседнего диагностика
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagnose_real_flight import (  # noqa: E402
    start_px4, start_python, kill_all_leftovers, clear_retained,
    TelemetryWatcher, publish_order, CHILDREN,
    G, R, Y, B, M, NC, CFG,
)

HOLD_S = 40.0
CRUISE_ALT = 40.0


def build_loiter_payload(veh_id: str) -> dict:
    base = {"lat": 43.0747, "lon": -89.3842, "alt": CRUISE_ALT}
    target = {"lat": 43.0762, "lon": -89.3820}
    speed = 8.0
    wps = [
        # transit к цели
        {"lat": target["lat"], "lon": target["lon"], "alt": CRUISE_ALT, "speed_m_s": speed},
        # ЗАВИСАНИЕ на цели — настоящий loiter
        {"lat": target["lat"], "lon": target["lon"], "alt": CRUISE_ALT, "speed_m_s": speed, "loiter_s": HOLD_S},
        # RTH
        {"lat": base["lat"], "lon": base["lon"], "alt": CRUISE_ALT, "speed_m_s": speed},
        {"lat": base["lat"], "lon": base["lon"], "alt": 10.0, "speed_m_s": 2.0},
        {"lat": base["lat"], "lon": base["lon"], "alt": 0.0, "speed_m_s": 2.0, "kind": "LAND"},
    ]
    return {
        "id": f"ord_diag_loiter_{int(time.time())}",
        "base": base,
        "addr1": {"lat": target["lat"], "lon": target["lon"], "alt": CRUISE_ALT},
        "addr2": {"lat": target["lat"], "lon": target["lon"], "alt": CRUISE_ALT},
        "payload_kg": 0.0,
        "priority": "normal",
        "extras": {
            "drone_id": veh_id,
            "mission_type": "isr",
            "cruise_alt_m": CRUISE_ALT,
            "cruise_speed_mps": speed,
            "takeoff_alt_m": 15.0,
            "auto_rth": True,
            "waypoints": wps,
        },
    }


def main():
    print(f"\n{B}{'='*72}{NC}")
    print(f"{B} LOITER DIAGNOSTIC · hold={HOLD_S}s @ {CRUISE_ALT}m{NC}")
    print(f"{B}{'='*72}{NC}\n")

    kill_all_leftovers()
    time.sleep(1.0)
    clear_retained()

    cfg = yaml.safe_load(CFG.read_text())
    drone0 = cfg["drones"][0]
    CHILDREN.append(start_px4(drone0))
    time.sleep(2.0)
    CHILDREN.append(start_python("bridge-0", "simulator.mavsdk_bridge", {"DRONE_ID": "0"}))
    time.sleep(5.0)
    CHILDREN.append(start_python("orch", "drone_core.workers.orchestrator"))
    time.sleep(3.0)

    watcher = TelemetryWatcher()
    watcher.start()
    time.sleep(1.0)

    publish_order(build_loiter_payload("veh_0"))
    print(f"\n{Y}[diag]{NC} loiter mission dispatched\n")

    start = time.time()
    DURATION = 200.0
    loiter_alts: list[float] = []
    prev_prog = (0, 0)
    loiter_window = False
    while time.time() - start < DURATION:
        snap = watcher.snapshot()
        elapsed = int(time.time() - start)
        cur, tot = watcher.last_progress
        # mission item индекс 1 (px4 idx) = loiter WP. progress.current==2 значит
        # достигнут loiter WP (current указывает на СЛЕДУЮЩИЙ после достигнутого).
        alt = watcher.alt_history[-1][1] if watcher.alt_history else 0.0
        # детектим окно зависания: дрон у цели, не двигается к новой точке
        in_loiter = (cur == 2 and tot >= 5)
        if in_loiter:
            loiter_window = True
            loiter_alts.append(alt)
        marker = f"  {M}<-- LOITER{NC}" if in_loiter else ""
        print(f"  [t+{elapsed:3d}s] {snap}{marker}")
        if watcher.last_mission_status == "COMPLETED":
            print(f"{G}[diag] COMPLETED at t+{elapsed}s{NC}")
            break
        time.sleep(2.0)

    print(f"\n{B}{'='*72}{NC}")
    print(f"{B} Summary{NC}")
    print(f"{B}{'='*72}{NC}")
    print(f"  Final status: {watcher.last_mission_status}  progress={watcher.last_progress}")
    if loiter_alts:
        amin, amax = min(loiter_alts), max(loiter_alts)
        print(f"  LOITER alt: samples={len(loiter_alts)} min={amin:.1f}m max={amax:.1f}m "
              f"drop={amax-amin:.1f}m  target={CRUISE_ALT}m")
        if amin < CRUISE_ALT - 8:
            print(f"  {R}!!! ALTITUDE LOSS during loiter: dropped to {amin:.1f}m (target {CRUISE_ALT}m) !!!{NC}")
        else:
            print(f"  {G}>>> Loiter held altitude OK <<<{NC}")
    else:
        print(f"  {R}No loiter samples captured (mission may have failed earlier){NC}")
    if watcher.alt_history:
        samples = watcher.alt_history[::max(1, len(watcher.alt_history)//16)][:16]
        print("  Alt timeline (s:m): " +
              ", ".join(f"{int(t-watcher.alt_history[0][0])}:{a:.0f}" for t, a in samples))

    watcher.stop()
    kill_all_leftovers()


if __name__ == "__main__":
    try:
        main()
    finally:
        kill_all_leftovers()
