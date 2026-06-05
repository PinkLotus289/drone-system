#!/usr/bin/env python3
"""Проверяем нативные команды MAVSDK как замену mission-loiter:
  1) goto_location + ожидание = «зависание на точке» (держит ли высоту?)
  2) do_orbit = ISR-орбита (держит ли высоту + реально кружит?)

Запускает свой PX4 instance 0, подключается напрямую по MAVSDK (без MQTT).
"""
from __future__ import annotations
import asyncio
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402
from diagnose_real_flight import start_px4, kill_all_leftovers, CFG, G, R, Y, B, M, NC  # noqa: E402
from mavsdk import System  # noqa: E402
from simulator.mavsdk_bridge import setup_sitl_params  # noqa: E402

HOME_LAT = 43.0747
HOME_LON = -89.3842
TARGET_LAT = 43.0762
TARGET_LON = -89.3820
CRUISE_ALT = 40.0
ORBIT_R = 30.0
ORBIT_VEL = 5.0


def hav(lat1, lon1, lat2, lon2):
    R_ = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R_*math.asin(math.sqrt(a))


async def alt_now(sys_):
    async for pos in sys_.telemetry.position():
        return pos.relative_altitude_m, pos.latitude_deg, pos.longitude_deg


async def sample(sys_, label, dur_s, center=None):
    """Снимает alt каждые 2с в течение dur_s. Если center задан — печатает радиус."""
    t0 = time.time()
    alts = []
    while time.time() - t0 < dur_s:
        alt, la, lo = await alt_now(sys_)
        alts.append(alt)
        extra = ""
        if center:
            r = hav(center[0], center[1], la, lo)
            extra = f"  r={r:.0f}m"
        print(f"    [{label} t+{int(time.time()-t0):2d}s] alt={alt:.1f}m{extra}")
        await asyncio.sleep(2.0)
    if alts:
        print(f"  {M}[{label}] alt min={min(alts):.1f} max={max(alts):.1f} drop={max(alts)-min(alts):.1f}m{NC}")
    return alts


async def run():
    cfg = yaml.safe_load(CFG.read_text())
    drone0 = cfg["drones"][0]
    kill_all_leftovers()
    await asyncio.sleep(1.0)
    px4 = start_px4(drone0)
    await asyncio.sleep(2.0)

    sys_ = System(port=50151)
    print(f"{Y}[native]{NC} connecting…")
    await sys_.connect(system_address="udp://:14540")
    async for st in sys_.core.connection_state():
        if st.is_connected:
            break
    print(f"{G}[native]{NC} connected")
    await setup_sitl_params(sys_, "[native]")

    async for h in sys_.telemetry.health():
        if h.is_global_position_ok and h.is_home_position_ok:
            break
    print(f"{G}[native]{NC} GPS ok")

    # --- takeoff ---
    await sys_.action.arm()
    await sys_.action.set_takeoff_altitude(CRUISE_ALT)
    await sys_.action.takeoff()
    print(f"{Y}[native]{NC} takeoff → {CRUISE_ALT}m")
    # ждём набора высоты
    t0 = time.time()
    while time.time() - t0 < 40:
        alt, _, _ = await alt_now(sys_)
        if alt >= CRUISE_ALT - 3:
            print(f"{G}[native]{NC} reached {alt:.1f}m")
            break
        await asyncio.sleep(1.0)

    # === TEST 1: goto + hold = hover ===
    print(f"\n{B}=== TEST 1: goto_location hover (hold 30s) ==={NC}")
    abs_alt = None
    async for pos in sys_.telemetry.position():
        abs_alt = pos.absolute_altitude_m
        break
    await sys_.action.goto_location(TARGET_LAT, TARGET_LON, abs_alt, 0.0)
    print(f"  goto target, abs_alt={abs_alt:.1f}")
    await asyncio.sleep(12.0)  # долететь
    hover_alts = await sample(sys_, "HOVER", 30.0, center=(TARGET_LAT, TARGET_LON))

    # === TEST 2: do_orbit = ISR orbit ===
    print(f"\n{B}=== TEST 2: do_orbit r={ORBIT_R}m v={ORBIT_VEL}m/s (30s) ==={NC}")
    abs_alt2 = None
    async for pos in sys_.telemetry.position():
        abs_alt2 = pos.absolute_altitude_m
        break
    try:
        from mavsdk.action import OrbitYawBehavior
        yb = OrbitYawBehavior.HOLD_FRONT_TO_CIRCLE_CENTER
    except Exception as e:
        print(f"  {R}OrbitYawBehavior import warn: {e}{NC}")
        yb = 0
    try:
        await sys_.action.do_orbit(ORBIT_R, ORBIT_VEL, yb, TARGET_LAT, TARGET_LON, abs_alt2)
        print(f"  do_orbit issued, abs_alt={abs_alt2:.1f}")
        orbit_alts = await sample(sys_, "ORBIT", 35.0, center=(TARGET_LAT, TARGET_LON))
    except Exception as e:
        print(f"  {R}do_orbit FAILED: {e!r}{NC}")
        orbit_alts = []

    # --- RTL ---
    print(f"\n{Y}[native]{NC} RTL")
    try:
        await sys_.action.return_to_launch()
    except Exception as e:
        print(f"  rtl warn: {e!r}")
    await asyncio.sleep(5.0)

    # --- verdict ---
    print(f"\n{B}{'='*60}{NC}")
    if hover_alts:
        ok = min(hover_alts) > CRUISE_ALT - 8
        print(f"  HOVER: min={min(hover_alts):.1f}m {'OK ✅' if ok else 'LOST ALT ❌'}")
    if orbit_alts:
        ok = min(orbit_alts) > CRUISE_ALT - 8
        print(f"  ORBIT: min={min(orbit_alts):.1f}m {'OK ✅' if ok else 'LOST ALT ❌'}")
    kill_all_leftovers()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    finally:
        kill_all_leftovers()
