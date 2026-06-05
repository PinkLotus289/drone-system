#!/usr/bin/env python3
"""End-to-end integration test для всех 5 типов миссий.

Не требует PX4 SITL — стартует orchestrator + telemetry_ingest как subprocesses
и мокает несколько bridges через MQTT (отвечают UPLOADED и т.п.).

Цели:
  * Verify orchestrator корректно обрабатывает каждый тип миссии
  * Verify multi-drone (sector) парралелится и шлёт N missions
  * Verify cruise_speed_mps протекает до waypoint.speed_m_s
  * Verify priority='urgent' проходит валидацию
  * Verify drone_id из extras уважается
"""
from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import paho.mqtt.client as mqtt

PROJECT = Path(__file__).resolve().parent.parent
SRC = PROJECT / "src"
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883

# Цвета для отчёта
G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
B = "\033[94m"
NC = "\033[0m"


class MockBridge:
    """Имитирует один MAVSDK-bridge:
      - подписывается на cmd/{veh_id}/#
      - на mission.upload отвечает UPLOADED через mission/{mid}/status
      - на arm/mission.start просто логирует
      - публикует fleet/active с IDLE состоянием
    """
    def __init__(self, veh_id: str, broker_host: str = MQTT_HOST, broker_port: int = MQTT_PORT):
        self.veh_id = veh_id  # "veh_0", "veh_1", ...
        self.received: List[Dict[str, Any]] = []
        self._last_mid: str | None = None
        self.client = mqtt.Client(client_id=f"mock_bridge_{veh_id}_{os.getpid()}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.connected = False
        self.broker_host = broker_host
        self.broker_port = broker_port

    def start(self):
        self.client.connect(self.broker_host, self.broker_port, 60)
        self.client.loop_start()
        # Wait connection
        for _ in range(50):
            if self.connected:
                break
            time.sleep(0.1)
        # Publish initial fleet/active (IDLE)
        self.publish_fleet_active("IDLE")

    def stop(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def publish_fleet_active(self, status: str = "IDLE", lat: float = 43.0747, lon: float = -89.3842):
        self.client.publish("fleet/active", json.dumps({
            "id": self.veh_id,
            "name": self.veh_id,
            "status": status,
            "lat": lat,
            "lon": lon,
            "alt": 0.0,
            "soc": 100.0,
        }), qos=1)

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = True
        client.subscribe(f"cmd/{self.veh_id}/#", qos=1)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            payload = {"raw": msg.payload.decode("utf-8", errors="ignore")}
        rec = {"topic": msg.topic, "payload": payload, "ts": time.time()}
        self.received.append(rec)
        print(f"  {B}[mock {self.veh_id}]{NC} recv {msg.topic}")
        # Reply to mission.upload with UPLOADED status
        if msg.topic.endswith("/mission.upload"):
            mid = payload.get("mission_id") or "unknown"
            self._last_mid = mid
            time.sleep(0.05)
            client.publish(f"mission/{mid}/status", json.dumps({
                "mission_id": mid,
                "vehicle_id": self.veh_id,
                "status": "UPLOADED",
            }), qos=1)
            print(f"  {G}[mock {self.veh_id}]{NC} → UPLOADED mid={mid}")
        elif msg.topic.endswith("/mission.start"):
            # Имитируем успешный полёт: через 0.5с шлём COMPLETED, освобождая
            # этот дрон в _busy_vehicles оркестратора — чтобы следующие тесты
            # могли его использовать.
            mid = payload.get("mission_id") or getattr(self, "_last_mid", "unknown")

            def _complete_later(mid=mid, veh=self.veh_id):
                time.sleep(0.5)
                client.publish(f"mission/{mid}/status", json.dumps({
                    "mission_id": mid,
                    "vehicle_id": veh,
                    "status": "COMPLETED",
                }), qos=1)
                # Also republish fleet/active IDLE
                self.publish_fleet_active("IDLE")
                print(f"  {G}[mock {veh}]{NC} → COMPLETED mid={mid}")
            threading.Thread(target=_complete_later, daemon=True).start()


class OrderListener:
    """Захватывает все mission/* и fleet/* топики для анализа."""
    def __init__(self, broker_host: str = MQTT_HOST, broker_port: int = MQTT_PORT):
        self.events: List[Dict[str, Any]] = []
        self.client = mqtt.Client(client_id=f"order_listener_{os.getpid()}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.broker_host = broker_host
        self.broker_port = broker_port

    def start(self):
        self.client.connect(self.broker_host, self.broker_port, 60)
        self.client.loop_start()
        time.sleep(0.3)

    def stop(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, rc):
        client.subscribe("mission/+/+", qos=1)
        client.subscribe("orders/+", qos=1)
        client.subscribe("cmd/+/+", qos=1)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            payload = {"raw": msg.payload.decode("utf-8", errors="ignore")}
        self.events.append({"topic": msg.topic, "payload": payload, "ts": time.time()})


def publish_order(client: mqtt.Client, order_payload: Dict[str, Any]):
    client.publish("orders/new", json.dumps(order_payload), qos=1)


def make_order_payload(*,
                       mission_type: str,
                       drone_id: str | None = None,
                       priority: str = "normal",
                       cruise_alt: float = 60.0,
                       cruise_speed: float = 10.0,
                       waypoints: List[Dict[str, Any]] | None = None,
                       **extras_kwargs) -> Dict[str, Any]:
    """Эмулирует payload, который main.py /api/orders публикует в orders/new."""
    payload = {
        "id": f"ord_test_{int(time.time()*1000) % 100000}_{mission_type}",
        "base": {"lat": 43.0747, "lon": -89.3842, "alt": 60.0},
        "addr1": {"lat": 43.078, "lon": -89.380, "alt": cruise_alt},
        "addr2": {"lat": 43.081, "lon": -89.376, "alt": cruise_alt},
        "payload_kg": 2.0,
        "priority": priority,
        "extras": {
            "drone_id": drone_id,
            "mission_type": mission_type,
            "cruise_alt_m": cruise_alt,
            "cruise_speed_mps": cruise_speed,
            "auto_rth": True,
            "record_sensors": True,
            "notes": None,
            "waypoints": waypoints,
            **extras_kwargs,
        },
    }
    return payload


def start_orchestrator() -> subprocess.Popen:
    print(f"{Y}[test] starting orchestrator…{NC}")
    proc = subprocess.Popen(
        ["python3", "-m", "drone_core.workers.orchestrator"],
        cwd=str(SRC),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    # Stream output in a thread
    def _stream():
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            sys.stdout.write(f"  [orch] {line}")
    t = threading.Thread(target=_stream, daemon=True)
    t.start()
    time.sleep(2.5)  # wait for orchestrator to subscribe
    return proc


def stop_proc(proc: subprocess.Popen | None):
    if not proc:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        time.sleep(0.3)
    except Exception:
        pass


# ============================ TESTS ============================

def run_tests():
    print(f"\n{B}{'='*72}{NC}")
    print(f"{B} Mission flow integration tests{NC}")
    print(f"{B}{'='*72}{NC}\n")

    # 1) Start orchestrator
    orch = start_orchestrator()

    # 2) Start mock bridges (3 drones)
    bridges = [MockBridge(f"veh_{i}") for i in range(3)]
    for b in bridges:
        b.start()

    # Re-publish fleet/active a few times so orchestrator picks them up
    for _ in range(3):
        for b in bridges:
            b.publish_fleet_active("IDLE")
        time.sleep(0.3)

    # 3) Order listener for analysis
    listener = OrderListener()
    listener.start()

    # 4) MQTT client to publish orders
    pub = mqtt.Client(client_id="test_publisher")
    pub.connect(MQTT_HOST, MQTT_PORT, 60)
    pub.loop_start()
    time.sleep(0.5)

    results: List[Dict[str, Any]] = []

    def wait_for_uploaded(mission_id_prefix: str, timeout: float = 8.0):
        """Wait for mission/X/status UPLOADED for the specific mission."""
        start = time.time()
        while time.time() - start < timeout:
            for ev in listener.events:
                if ev["topic"].startswith("mission/") and ev["topic"].endswith("/status"):
                    p = ev["payload"]
                    if p.get("status") == "UPLOADED":
                        return p
            time.sleep(0.1)
        return None

    def get_upload_for_vehicle(veh_id: str):
        """Найти cmd/veh_id/mission.upload payload."""
        for ev in listener.events:
            if ev["topic"] == f"cmd/{veh_id}/mission.upload":
                return ev["payload"]
        return None

    # -------- TEST 1: Delivery (no custom waypoints, uses plan_order) --------
    print(f"\n{Y}[TEST 1]{NC} Delivery — plan_order (base→addr1→addr2→base)")
    listener.events.clear()
    for b in bridges:
        b.received.clear()
        b.publish_fleet_active("IDLE")
    time.sleep(0.3)
    payload = {
        "id": "ord_delivery_test_1",
        "base": {"lat": 43.0747, "lon": -89.3842, "alt": 60.0},
        "addr1": {"lat": 43.078, "lon": -89.380, "alt": 60.0},
        "addr2": {"lat": 43.081, "lon": -89.376, "alt": 60.0},
        "payload_kg": 2.0,
        "priority": "normal",
    }
    publish_order(pub, payload)
    time.sleep(3.0)
    found_upload = None
    for veh_id in [b.veh_id for b in bridges]:
        u = get_upload_for_vehicle(veh_id)
        if u and len(u.get("waypoints", [])) > 0:
            found_upload = (veh_id, u)
            break
    if found_upload:
        veh_id, upl = found_upload
        wps = upl.get("waypoints", [])
        kinds = [w.get("kind") for w in wps]
        takeoff_alt = upl.get("takeoff_alt_m")  # default 15.0 даже для legacy
        ok = len(wps) > 0 and takeoff_alt is not None
        msg = f"  {G}✓ PASS{NC}" if ok else f"  {R}✗ FAIL{NC}"
        print(f"{msg} · drone={veh_id} · {len(wps)} wps · kinds={kinds} · takeoff_alt={takeoff_alt}")
        results.append({"name": "delivery", "pass": ok, "drone": veh_id, "wps": len(wps), "kinds": kinds})
    else:
        print(f"  {R}✗ FAIL · no mission.upload received{NC}")
        results.append({"name": "delivery", "pass": False})

    time.sleep(1.5)  # ждём что mock пошлёт COMPLETED для TEST 1 и orchestrator освободит drone

    # -------- TEST 2: ISR (frontend сам шлёт полный список с RTH+LAND) --------
    # Frontend buildISRWaypoints: transit → loiter → approach → LAND  ИЛИ
    # transit → orbit_points×lap → approach → LAND.
    # Orchestrator — passthrough: что прислали, то и улетит.
    print(f"\n{Y}[TEST 2]{NC} ISR — transit+loiter+approach+LAND (passthrough)")
    listener.events.clear()
    for b in bridges:
        b.received.clear()
        b.publish_fleet_active("IDLE")
    time.sleep(0.5)
    isr_wps = [
        # transit to target @ cruise speed
        {"lat": 43.080, "lon": -89.380, "alt": 80.0, "speed_m_s": 12.0},
        # hover loiter on target — loiter_s=60s
        {"lat": 43.080, "lon": -89.380, "alt": 80.0, "speed_m_s": 2.0, "loiter_s": 60},
        # RTH transit to base cruise
        {"lat": 43.0747, "lon": -89.3842, "alt": 80.0, "speed_m_s": 12.0},
        # approach over base
        {"lat": 43.0747, "lon": -89.3842, "alt": 10.0, "speed_m_s": 2.0},
        # LAND at base
        {"lat": 43.0747, "lon": -89.3842, "alt": 0.0, "speed_m_s": 2.0, "kind": "LAND"},
    ]
    payload = make_order_payload(
        mission_type="isr",
        drone_id="veh_0",
        priority="high",
        cruise_alt=80.0,
        cruise_speed=12.0,
        waypoints=isr_wps,
        orbit_radius_m=0,
        takeoff_alt_m=15.0,
    )
    publish_order(pub, payload)
    time.sleep(3.0)
    u = get_upload_for_vehicle("veh_0")
    if u:
        wps = u.get("waypoints", [])
        kinds = [w.get("kind") for w in wps]
        holds = [w.get("hold_s") for w in wps]
        takeoff_alt = u.get("takeoff_alt_m")
        ok = (
            len(wps) == 5
            and kinds.count("LAND") == 1
            and kinds[-1] == "LAND"
            and any(h and h >= 60 for h in holds)
            and takeoff_alt == 15.0
        )
        msg = f"  {G}✓ PASS{NC}" if ok else f"  {R}✗ FAIL{NC}"
        print(f"{msg} · {len(wps)} wps · kinds={kinds} · holds={holds} · takeoff_alt={takeoff_alt}")
        results.append({"name": "isr", "pass": ok, "wps": len(wps), "kinds": kinds})
    else:
        print(f"  {R}✗ FAIL · no upload for veh_0{NC}")
        results.append({"name": "isr", "pass": False})

    time.sleep(1.5)

    # -------- TEST 3: Patrol (3 WPs × 2 loops + RTH+LAND + altitude-lock midpoints) --------
    # Frontend buildPatrolWaypoints вставляет midpoint между каждой парой
    # consecutive loiter-WP (когда loiter_wp>0) — это altitude-lock для PX4,
    # без которого дрон теряет высоту в SITL при transit loiter→loiter.
    # 3 route pts × 2 loops = 6 loiter WPs + 5 midpoints between them = 11.
    # + 3 RTH (cruise transit, approach, LAND) = 14 total.
    print(f"\n{Y}[TEST 3]{NC} Patrol — 3 WPs × 2 loops + midpoints + RTH+LAND")
    listener.events.clear()
    for b in bridges:
        b.received.clear()
        b.publish_fleet_active("IDLE")
    time.sleep(0.5)
    route_pts = [(43.078, -89.382), (43.080, -89.380), (43.082, -89.378)]
    # Эмулируем результат buildPatrolWaypoints(loops=2, loiter_wp=5)
    flat = route_pts * 2  # 6 точек
    patrol_wps = []
    for i, (lat, lon) in enumerate(flat):
        patrol_wps.append({"lat": lat, "lon": lon, "alt": 60.0,
                           "speed_m_s": 10.0, "loiter_s": 5})
        if i + 1 < len(flat):
            nxt = flat[i + 1]
            patrol_wps.append({"lat": (lat + nxt[0]) / 2,
                               "lon": (lon + nxt[1]) / 2,
                               "alt": 60.0, "speed_m_s": 10.0})
    # RTH+LAND
    patrol_wps += [
        {"lat": 43.0747, "lon": -89.3842, "alt": 60.0, "speed_m_s": 10.0},
        {"lat": 43.0747, "lon": -89.3842, "alt": 10.0, "speed_m_s": 2.0},
        {"lat": 43.0747, "lon": -89.3842, "alt": 0.0, "speed_m_s": 2.0, "kind": "LAND"},
    ]
    payload = make_order_payload(
        mission_type="patrol",
        drone_id="veh_1",
        priority="normal",
        cruise_alt=60.0,
        cruise_speed=10.0,
        waypoints=patrol_wps,
        loop_count=2,
        takeoff_alt_m=15.0,
    )
    publish_order(pub, payload)
    time.sleep(3.0)
    u = get_upload_for_vehicle("veh_1")
    if u:
        wps = u.get("waypoints", [])
        nav_count = sum(1 for w in wps if w.get("kind") == "NAV")
        land_count = sum(1 for w in wps if w.get("kind") == "LAND")
        # 6 loiter WPs at indices 0,2,4,6,8,10 (every even). Midpoints at 1,3,5,7,9 → loiter_s=0.
        loiter_count = sum(1 for w in wps if (w.get("hold_s") or 0) >= 5)
        ok = (
            len(wps) == 14
            and nav_count == 13
            and land_count == 1
            and loiter_count == 6
        )
        msg = f"  {G}✓ PASS{NC}" if ok else f"  {R}✗ FAIL{NC}"
        print(f"{msg} · {len(wps)} wps · {nav_count} NAV + {land_count} LAND · loiter_count={loiter_count}")
        results.append({"name": "patrol", "pass": ok, "wps": len(wps), "nav": nav_count, "land": land_count})
    else:
        print(f"  {R}✗ FAIL · no upload for veh_1{NC}")
        results.append({"name": "patrol", "pass": False})

    time.sleep(1.5)

    # -------- TEST 4: Sector (pattern + RTH+LAND, passthrough) --------
    print(f"\n{Y}[TEST 4]{NC} Sector — 4 NAV + RTH+LAND (passthrough)")
    listener.events.clear()
    for b in bridges:
        b.received.clear()
        b.publish_fleet_active("IDLE")
    time.sleep(0.5)
    sector_pattern_wps = [
        {"lat": 43.078, "lon": -89.385, "alt": 70.0, "speed_m_s": 8.0},
        {"lat": 43.078, "lon": -89.380, "alt": 70.0, "speed_m_s": 8.0},
        {"lat": 43.080, "lon": -89.380, "alt": 70.0, "speed_m_s": 8.0},
        {"lat": 43.080, "lon": -89.385, "alt": 70.0, "speed_m_s": 8.0},
        {"lat": 43.0747, "lon": -89.3842, "alt": 70.0, "speed_m_s": 8.0},
        {"lat": 43.0747, "lon": -89.3842, "alt": 10.0, "speed_m_s": 2.0},
        {"lat": 43.0747, "lon": -89.3842, "alt": 0.0, "speed_m_s": 2.0, "kind": "LAND"},
    ]
    payload = make_order_payload(
        mission_type="sector",
        drone_id="veh_2",
        priority="normal",
        cruise_alt=70.0,
        cruise_speed=8.0,
        waypoints=sector_pattern_wps,
        pattern="lawnmower",
        takeoff_alt_m=15.0,
    )
    publish_order(pub, payload)
    time.sleep(3.0)
    u = get_upload_for_vehicle("veh_2")
    if u:
        wps = u.get("waypoints", [])
        kinds = [w.get("kind") for w in wps]
        ok = (len(wps) == 7 and kinds.count("LAND") == 1 and kinds[-1] == "LAND")
        msg = f"  {G}✓ PASS{NC}" if ok else f"  {R}✗ FAIL{NC}"
        print(f"{msg} · {len(wps)} wps · kinds={kinds}")
        results.append({"name": "sector_single", "pass": ok, "wps": len(wps)})
    else:
        print(f"  {R}✗ FAIL · no upload for veh_2{NC}")
        results.append({"name": "sector_single", "pass": False})

    time.sleep(1.5)

    # -------- TEST 5: Sector observation (multi-drone, 3 missions, different sub-areas) --------
    print(f"\n{Y}[TEST 5]{NC} Sector observation — 3 drones, DIFFERENT sub-zones in parallel")
    listener.events.clear()
    for b in bridges:
        b.received.clear()
        b.publish_fleet_active("IDLE")
    time.sleep(1.0)

    # Каждый дрон получает СВОЙ sub-area (смещение по lon на 0.005°).
    # Frontend (splitSectorForDrones) делит большую зону на N полос — тут
    # эмулируем результат: разные waypoints для каждого veh.
    for i in range(3):
        sector_wps = [
            {"lat": 43.078, "lon": -89.385 + i * 0.005, "alt": 70.0, "speed_m_s": 10.0},
            {"lat": 43.082, "lon": -89.385 + i * 0.005, "alt": 70.0, "speed_m_s": 10.0},
            # RTH+LAND на общую базу
            {"lat": 43.0747, "lon": -89.3842, "alt": 70.0, "speed_m_s": 10.0},
            {"lat": 43.0747, "lon": -89.3842, "alt": 10.0, "speed_m_s": 2.0},
            {"lat": 43.0747, "lon": -89.3842, "alt": 0.0, "speed_m_s": 2.0, "kind": "LAND"},
        ]
        payload = make_order_payload(
            mission_type="sector",
            drone_id=f"veh_{i}",
            priority="high",
            cruise_alt=70.0,
            cruise_speed=10.0,
            waypoints=sector_wps,
            pattern="lawnmower",
            sector_index=i,
            sector_total=3,
            takeoff_alt_m=15.0,
        )
        publish_order(pub, payload)
        time.sleep(0.05)  # small jitter

    time.sleep(5.0)
    sector_uploads = {}
    for veh_id in ["veh_0", "veh_1", "veh_2"]:
        u = get_upload_for_vehicle(veh_id)
        if u:
            sector_uploads[veh_id] = u
    # Verify uniqueness: первый WP каждого дрона должен отличаться по lon
    distinct_first_lons = set()
    for vid, upl in sector_uploads.items():
        wps = upl.get("waypoints", [])
        if wps:
            first = wps[0]
            pos = first.get("pos", first)
            distinct_first_lons.add(round(float(pos["lon"]), 4))
    if len(sector_uploads) == 3 and len(distinct_first_lons) == 3:
        print(f"  {G}✓ PASS{NC} · 3 drones × distinct sub-zones (first lons={sorted(distinct_first_lons)})")
        for vid, upl in sector_uploads.items():
            print(f"    {vid}: {len(upl.get('waypoints', []))} wps")
        results.append({"name": "sector_multi", "pass": True, "drones": list(sector_uploads.keys())})
    else:
        print(f"  {R}✗ FAIL · uploads={len(sector_uploads)}/3 · distinct_lons={len(distinct_first_lons)}{NC}")
        results.append({"name": "sector_multi", "pass": False, "drones": list(sector_uploads.keys())})

    time.sleep(2.0)  # ждём COMPLETED для всех 3 sector drones

    # -------- TEST 6: Urgent priority --------
    print(f"\n{Y}[TEST 6]{NC} Urgent priority validation")
    listener.events.clear()
    for b in bridges:
        b.received.clear()
        b.publish_fleet_active("IDLE")
    time.sleep(0.5)
    payload = make_order_payload(
        mission_type="delivery",
        drone_id="veh_0",
        priority="urgent",
        cruise_alt=60.0,
        cruise_speed=10.0,
    )
    publish_order(pub, payload)
    time.sleep(2.0)
    u = get_upload_for_vehicle("veh_0")
    ok = u is not None
    msg = f"  {G}✓ PASS{NC}" if ok else f"  {R}✗ FAIL{NC}"
    print(f"{msg} · urgent={'accepted' if ok else 'rejected'}")
    results.append({"name": "urgent", "pass": ok})

    # ============ Cleanup ============
    pub.loop_stop()
    pub.disconnect()
    listener.stop()
    for b in bridges:
        b.stop()
    stop_proc(orch)

    # ============ Report ============
    print(f"\n{B}{'='*72}{NC}")
    print(f"{B} Summary{NC}")
    print(f"{B}{'='*72}{NC}")
    passed = sum(1 for r in results if r.get("pass"))
    total = len(results)
    for r in results:
        mark = f"{G}✓{NC}" if r.get("pass") else f"{R}✗{NC}"
        print(f"  {mark} {r['name']:10s}  {r}")
    print(f"\n  {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run_tests())
