from __future__ import annotations
import math
from typing import TYPE_CHECKING, List, Tuple
from drone_core.domain.models import Order, Mission, Waypoint, LLA, MissionStatus

if TYPE_CHECKING:
    from drone_core.infra.terrain import TerrainService, TerrainResult


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

    # Перед LAND добавляем approach-waypoint над базой на малой высоте (10 м):
    # без него PX4/MAVSDK LAND-action приземляется в текущих координатах
    # дрона, а не на базе — дрон «садится где попало».
    approach_alt = 10.0
    # hold_s=0: без зависания в точках. В PX4 v1.17-alpha1+SIH hold запускает
    # landing-detector-false-positive через несколько секунд и дрон садится
    # посреди маршрута. Fly-through решает проблему.
    wps: List[Waypoint] = [
        Waypoint(pos=LLA(lat=base.lat, lon=base.lon, alt=base.alt), kind="TAKEOFF"),
        Waypoint(pos=LLA(lat=a1.lat, lon=a1.lon, alt=a1.alt), kind="NAV", hold_s=0.0),
        Waypoint(pos=LLA(lat=a2.lat, lon=a2.lon, alt=a2.alt), kind="NAV", hold_s=0.0),
        Waypoint(pos=LLA(lat=base.lat, lon=base.lon, alt=approach_alt), kind="NAV", hold_s=0.0),
        Waypoint(pos=LLA(lat=base.lat, lon=base.lon, alt=0.0), kind="LAND"),
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


async def apply_terrain_agl(
    waypoints: List[Waypoint], terrain: "TerrainService"
) -> Tuple[List[Waypoint], "TerrainResult"]:
    """Adjust waypoint altitudes from AGL to MSL by adding SRTM terrain elevation.

    Each waypoint's current alt is treated as desired height above ground (AGL).
    After this call, alt = terrain_msl + agl_offset, ready for PX4 mission upload.

    Returns the adjusted waypoints AND a TerrainResult so the caller can tell
    whether the terrain data was complete or degraded (fell back to nearest-known
    / flat 0 m). On degraded data the operator must be warned — flying an AGL
    offset on top of a wrong 0 m MSL reference means flying into the ground.
    """
    locations = [(wp.pos.lat, wp.pos.lon) for wp in waypoints]
    res = await terrain.get_elevations_meta(locations)
    adjusted: List[Waypoint] = []
    for wp, terrain_m in zip(waypoints, res.elevations):
        new_pos = wp.pos.model_copy(update={"alt": terrain_m + wp.pos.alt})
        adjusted.append(wp.model_copy(update={"pos": new_pos}))
    return adjusted, res


# Waypoint kinds we are allowed to lift to keep terrain clearance. TAKEOFF /
# LAND / RTL legs are climb/descent near home and must stay at their planned
# altitude (lifting a LAND point would stop the drone from landing).
_LIFTABLE = {"NAV", "ORBIT"}


async def enforce_clearance(
    waypoints: List[Waypoint],
    terrain: "TerrainService",
    min_clearance_m: float = 25.0,
    step_m: float = 120.0,
) -> Tuple[List[Waypoint], dict]:
    """Guarantee a minimum height above ground *along each leg*, not just at waypoints.

    apply_terrain_agl only fixes altitude at the waypoints themselves; a straight
    leg between two points can still cut through a hill in between. This samples
    terrain every ``step_m`` along every leg and, where the interpolated cruise
    altitude would come within ``min_clearance_m`` of the ground, lifts the
    liftable (NAV/ORBIT) endpoints so the whole leg clears.

    Expects waypoint altitudes already in MSL (call after apply_terrain_agl).
    Returns adjusted waypoints + a report dict for logging / UI.
    """
    from drone_core.infra.terrain import haversine_m, interpolate

    n = len(waypoints)
    report = {"checked_legs": 0, "skipped_legs": 0, "violations": 0,
              "lifted_waypoints": 0, "max_lift_m": 0.0,
              "min_clearance_m": min_clearance_m, "unresolved_violations": 0}
    if n < 2:
        return waypoints, report

    # 1) Build per-leg sample locations (endpoints included), remember the flat
    #    index range so we can map elevations back after one batched fetch.
    leg_samples: List[List[int]] = []
    all_locs: List[Tuple[float, float]] = []
    for i in range(n - 1):
        a, b = waypoints[i].pos, waypoints[i + 1].pos
        d = haversine_m(a.lat, a.lon, b.lat, b.lon)
        steps = max(1, int(d // step_m))
        idxs = []
        for k in range(steps + 1):
            lat, lon = interpolate(a.lat, a.lon, b.lat, b.lon, k / steps)
            idxs.append(len(all_locs))
            all_locs.append((lat, lon))
        leg_samples.append(idxs)

    res = await terrain.get_elevations_meta(all_locs)
    ground = res.elevations

    # 2) Required floor per waypoint. We only ENFORCE clearance on cruise legs
    #    (both endpoints liftable). Legs touching TAKEOFF/LAND/RTL are climb or
    #    descent: their ground endpoint sits at terrain by design, so measuring
    #    full clearance there would always "fail" on a normal approach. For those
    #    we only raise an UNRESOLVED hazard when terrain actually pokes through
    #    the straight climb/descent line between the endpoints (a real obstacle
    #    the autopilot's straight path can't dodge), checking interior samples
    #    only so the at-ground endpoint itself never trips the alarm.
    _COLLISION_BUFFER_M = 1.0
    required = [0.0] * n
    for i in range(n - 1):
        a_lift = waypoints[i].kind in _LIFTABLE
        b_lift = waypoints[i + 1].kind in _LIFTABLE
        if a_lift and b_lift:
            report["checked_legs"] += 1
            leg_floor = max(ground[j] for j in leg_samples[i]) + min_clearance_m
            planned_min = min(waypoints[i].pos.alt, waypoints[i + 1].pos.alt)
            if planned_min < leg_floor:
                report["violations"] += 1
            required[i] = max(required[i], leg_floor)
            required[i + 1] = max(required[i + 1], leg_floor)
        else:
            report["skipped_legs"] += 1
            alt_a, alt_b = waypoints[i].pos.alt, waypoints[i + 1].pos.alt
            idxs = leg_samples[i]
            steps = len(idxs) - 1
            for kk, j in enumerate(idxs):
                if kk == 0 or kk == steps:
                    continue  # endpoints sit at ground by design
                planned = alt_a + (alt_b - alt_a) * (kk / steps)
                if ground[j] > planned + _COLLISION_BUFFER_M:
                    report["unresolved_violations"] += 1
                    break

    # 3) Lift liftable waypoints to their required floor.
    adjusted: List[Waypoint] = []
    for i, wp in enumerate(waypoints):
        if wp.kind in _LIFTABLE and wp.pos.alt < required[i]:
            lift = required[i] - wp.pos.alt
            report["lifted_waypoints"] += 1
            report["max_lift_m"] = max(report["max_lift_m"], lift)
            adjusted.append(wp.model_copy(update={"pos": wp.pos.model_copy(update={"alt": required[i]})}))
        else:
            adjusted.append(wp)

    report["terrain_degraded"] = res.degraded
    return adjusted, report
