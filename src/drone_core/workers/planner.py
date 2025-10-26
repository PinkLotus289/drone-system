from __future__ import annotations
import math
from typing import List
from drone_core.domain.models import Order, Mission, Waypoint, LLA, MissionStatus


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def plan_order(order: Order, cruise_mps: float = 10.0) -> Mission:
    """
    Прямолинейный маршрут (MVP): base -> addr1 -> addr2 -> base.
    """
    base = order.base
    a1 = order.addr1
    a2 = order.addr2

    wps: List[Waypoint] = [
        Waypoint(pos=LLA(lat=base.lat, lon=base.lon, alt=base.alt), kind="TAKEOFF"),
        Waypoint(pos=LLA(lat=a1.lat, lon=a1.lon, alt=a1.alt), kind="NAV", hold_s=3.0),
        Waypoint(pos=LLA(lat=a2.lat, lon=a2.lon, alt=a2.alt), kind="NAV", hold_s=3.0),
        Waypoint(pos=LLA(lat=base.lat, lon=base.lon, alt=base.alt), kind="LAND"),
    ]

    d = 0.0
    d += _haversine_m(base.lat, base.lon, a1.lat, a1.lon)
    d += _haversine_m(a1.lat, a1.lon, a2.lat, a2.lon)
    d += _haversine_m(a2.lat, a2.lon, base.lat, base.lon)

    # грубо ETA = время полёта + 60с на взлёт/посадку/манёвры
    eta_s = d / max(cruise_mps, 0.1) + 60.0

    m = Mission(
        payload_kg=order.payload_kg,
        priority=order.priority,
        pickup=a1,    # для совместимости
        dropoff=a2,   # для совместимости
        waypoints=wps,
        status=MissionStatus.PLANNED,
    )
    return m
