#!/usr/bin/env python3
"""E2E проверка ISR/Patrol через полный стек (PX4 + bridge + orchestrator) с
НОВОЙ нативной логикой. Payload'ы повторяют формат buildISRWaypoints /
buildPatrolWaypoints из app.js.

Использование: python3 tests/diag_e2e.py [isr|patrol]
"""
from __future__ import annotations
import json
import math
import os
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagnose_real_flight import (  # noqa: E402
    start_px4, start_python, kill_all_leftovers, clear_retained, publish_order,
    CHILDREN, G, R, Y, B, M, NC, CFG,
)

BASE = {"lat": 43.0747, "lon": -89.3842}
TARGET = {"lat": 43.0762, "lon": -89.3820}
CRUISE_ALT = 40.0
CRUISE_SPEED = 10.0
ORBIT_R = 35.0
LOITER_S = 45.0


def hav(a, b, c, d):
    R_ = 6371000.0
    p1, p2 = math.radians(a), math.radians(c)
    dphi, dl = math.radians(c - a), math.radians(d - b)
    x = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R_*math.asin(math.sqrt(x))


def _rth_land():
    return [
        {"lat": BASE["lat"], "lon": BASE["lon"], "alt": CRUISE_ALT, "speed_m_s": CRUISE_SPEED},
        {"lat": BASE["lat"], "lon": BASE["lon"], "alt": 10.0, "speed_m_s": 2.0},
        {"lat": BASE["lat"], "lon": BASE["lon"], "alt": 0.0, "kind": "LAND", "speed_m_s": 2.0},
    ]


def build_isr():
    return [
        {"lat": TARGET["lat"], "lon": TARGET["lon"], "alt": CRUISE_ALT, "speed_m_s": CRUISE_SPEED},
        {"lat": TARGET["lat"], "lon": TARGET["lon"], "alt": CRUISE_ALT, "kind": "ORBIT",
         "orbit_radius_m": ORBIT_R, "speed_m_s": 5.0, "loiter_s": LOITER_S},
        *_rth_land(),
    ]


def build_patrol():
    route = [(43.0758, -89.3832), (43.0768, -89.3822), (43.0760, -89.3808)]
    wps = []
    for _loop in range(2):
        for lat, lon in route:
            wps.append({"lat": lat, "lon": lon, "alt": CRUISE_ALT,
                        "speed_m_s": CRUISE_SPEED, "loiter_s": 12})
    wps += _rth_land()
    return wps


def build_delivery():
    # Повторяет buildDeliveryWaypoints с ЕДИНОЙ скоростью (фикс бага 2 м/с):
    # все точки speed=CRUISE; спуски вертикальные.
    pickup = (43.0760, -89.3820)
    drop = (43.0770, -89.3808)
    S = CRUISE_SPEED
    return [
        {"lat": pickup[0], "lon": pickup[1], "alt": CRUISE_ALT, "speed_m_s": S},
        {"lat": pickup[0], "lon": pickup[1], "alt": 10.0, "speed_m_s": S, "loiter_s": 3},
        {"lat": pickup[0], "lon": pickup[1], "alt": CRUISE_ALT, "speed_m_s": S},
        {"lat": drop[0], "lon": drop[1], "alt": CRUISE_ALT, "speed_m_s": S},   # cruise leg — был баг 2 м/с
        {"lat": drop[0], "lon": drop[1], "alt": 10.0, "speed_m_s": S, "loiter_s": 3},
        {"lat": drop[0], "lon": drop[1], "alt": CRUISE_ALT, "speed_m_s": S},
        *_rth_land(),
    ]


def make_payload(mtype, wps, takeoff_profile="vertical"):
    return {
        "id": f"ord_e2e_{mtype}_{int(time.time())}",
        "base": {"lat": BASE["lat"], "lon": BASE["lon"], "alt": CRUISE_ALT},
        "addr1": {"lat": TARGET["lat"], "lon": TARGET["lon"], "alt": CRUISE_ALT},
        "addr2": {"lat": TARGET["lat"], "lon": TARGET["lon"], "alt": CRUISE_ALT},
        "payload_kg": 0.0,
        "priority": "normal",
        "extras": {
            "drone_id": "veh_0",
            "mission_type": mtype,
            "cruise_alt_m": CRUISE_ALT,
            "cruise_speed_mps": CRUISE_SPEED,
            "takeoff_alt_m": 15.0,
            "takeoff_profile": takeoff_profile,
            "auto_rth": True,
            "waypoints": wps,
        },
    }


class W:
    def __init__(self):
        self.lat = self.lon = self.alt = 0.0
        self.gs = 0.0
        self.prog = (0, 0)
        self.fleet = None
        self.mstatus = None
        self.c = mqtt.Client(client_id=f"e2e_{os.getpid()}")
        self.c.on_connect = lambda cl, *_: (
            cl.subscribe("telem/+/pose"), cl.subscribe("telem/+/velocity"),
            cl.subscribe("mission/+/progress"),
            cl.subscribe("mission/+/status"), cl.subscribe("fleet/active"))
        self.c.on_message = self._m

    def _m(self, cl, u, msg):
        try:
            p = json.loads(msg.payload.decode())
        except Exception:
            return
        if msg.topic.endswith("/pose"):
            self.lat, self.lon, self.alt = p.get("lat", 0), p.get("lon", 0), p.get("alt", 0)
        elif msg.topic.endswith("/velocity"):
            self.gs = p.get("gs", 0)
        elif msg.topic.endswith("/progress"):
            self.prog = (p.get("current", 0), p.get("total", 0))
        elif msg.topic.endswith("/status"):
            self.mstatus = p.get("status")
        elif msg.topic == "fleet/active":
            self.fleet = p.get("status")

    def start(self):
        self.c.connect("127.0.0.1", 1883, 60)
        self.c.loop_start()

    def stop(self):
        self.c.loop_stop(); self.c.disconnect()


def main():
    mtype = sys.argv[1] if len(sys.argv) > 1 else "isr"
    DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 300.0
    takeoff_profile = sys.argv[3] if len(sys.argv) > 3 else "vertical"
    builder = {"isr": build_isr, "patrol": build_patrol, "delivery": build_delivery}[mtype]
    wps = builder()
    print(f"\n{B}{'='*72}\n E2E · {mtype.upper()} · {len(wps)} wps · takeoff={takeoff_profile}\n{'='*72}{NC}\n")

    kill_all_leftovers(); time.sleep(1.0); clear_retained()
    cfg = yaml.safe_load(CFG.read_text())
    CHILDREN.append(start_px4(cfg["drones"][0])); time.sleep(2.0)
    CHILDREN.append(start_python("bridge-0", "simulator.mavsdk_bridge", {"DRONE_ID": "0"})); time.sleep(5.0)
    CHILDREN.append(start_python("orch", "drone_core.workers.orchestrator")); time.sleep(3.0)

    w = W(); w.start(); time.sleep(1.0)
    publish_order(make_payload(mtype, wps, takeoff_profile))
    print(f"{Y}[e2e]{NC} {mtype} dispatched\n")

    start = time.time()
    orbit_radii, hover_alts = [], []
    min_alt_air = 99.0
    cruise_gs = []           # gs на горизонтальных крейсерских участках (alt≈cruise)
    climb_track = []         # (horiz_dist_from_base, alt) пока alt<cruise — профиль взлёта
    base_started = None
    while time.time() - start < DURATION:
        el = int(time.time() - start)
        r = hav(TARGET["lat"], TARGET["lon"], w.lat, w.lon) if w.lat else 0
        cur, tot = w.prog
        extra = ""
        if mtype == "isr" and cur == 1 and w.alt > 5:
            orbit_radii.append(r); hover_alts.append(w.alt)
            extra = f"  {M}ORBIT r={r:.0f}m{NC}"
        # Профиль взлёта: меряем от ФАКТИЧЕСКОЙ точки старта дрона (его home),
        # а не от BASE (они смещены). hd≈const при наборе = вертикальный взлёт.
        if w.lat and base_started is None and 0.0 < w.alt < 2.0:
            base_started = (w.lat, w.lon)
        if base_started and w.alt < CRUISE_ALT - 3 and el < 60:
            hd = hav(base_started[0], base_started[1], w.lat, w.lon)
            climb_track.append((round(hd, 1), round(w.alt, 1)))
        # gs на круизе (alt близко к cruise, горизонтальный полёт)
        if w.alt > CRUISE_ALT - 6 and w.gs > 0.5:
            cruise_gs.append(w.gs)
        if w.alt > 5 and el > 15:
            min_alt_air = min(min_alt_air, w.alt)
        print(f"  [t+{el:3d}s] alt={w.alt:5.1f}m gs={w.gs:4.1f} r={r:4.0f}m prog={cur}/{tot} fleet={w.fleet} mis={w.mstatus}{extra}")
        if w.mstatus == "COMPLETED":
            print(f"{G}[e2e] COMPLETED at t+{el}s{NC}"); break
        time.sleep(2.0)

    print(f"\n{B}{'='*72}\n Summary · {mtype} · takeoff={takeoff_profile}\n{'='*72}{NC}")
    print(f"  final: status={w.mstatus} prog={w.prog} min_alt_airborne={min_alt_air:.1f}m (cruise={CRUISE_ALT})")
    if cruise_gs:
        cruise_gs.sort()
        med = cruise_gs[len(cruise_gs)//2]
        print(f"  CRUISE gs: samples={len(cruise_gs)} median={med:.1f} max={max(cruise_gs):.1f} m/s "
              f"(target ~{CRUISE_SPEED}) {'✅' if med > CRUISE_SPEED*0.6 else '❌ too slow'}")
    if mtype == "isr" and orbit_radii:
        print(f"  ORBIT: samples={len(orbit_radii)} r_avg={sum(orbit_radii)/len(orbit_radii):.1f}m "
              f"(target {ORBIT_R}m) alt_min={min(hover_alts):.1f}m")
    if climb_track:
        # max гор.дистанция пока alt<cruise: ~0 = вертикальный, большой = наклонный
        max_hd = max(hd for hd, _ in climb_track)
        print(f"  TAKEOFF climb track (hd→alt): {climb_track[:8]}")
        print(f"  max horiz dist while climbing to cruise = {max_hd:.1f}m "
              f"→ {'VERTICAL (climb then go)' if max_hd < 15 else 'INCLINED (climb en-route)'}")
    print(f"  {'✅ COMPLETED' if w.mstatus == 'COMPLETED' else '❌ NOT COMPLETED'}")
    w.stop(); kill_all_leftovers()


if __name__ == "__main__":
    try:
        main()
    finally:
        kill_all_leftovers()
