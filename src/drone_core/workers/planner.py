from __future__ import annotations
import math
import asyncio
import logging
from drone_core.infra.repositories import make_repos
from drone_core.domain.models import MissionStatus, Waypoint, LLA

log = logging.getLogger("planner")


class Planner:
    """Упрощённый планировщик маршрутов (MVP)."""

    def __init__(self):
        self.fleet_repo, self.missions_repo = make_repos()

    async def plan_mission(self, mission_id: str):
        """Построить маршрут для миссии (home → pickup → drop → home_rtl)."""
        mission = await self.missions_repo.get(mission_id)
        if not mission:
            log.error(f"Mission {mission_id} not found")
            return None

        # Выбор дрона (просто первый свободный)
        free = await self.fleet_repo.list_free()
        if not free:
            log.warning("No free vehicles to assign")
            return None
        vehicle = free[0]

        # Назначаем дрон
        await self.missions_repo.assign_vehicle(mission.id, vehicle.id)
        await self.fleet_repo.set_status(vehicle.id, vehicle.status.BUSY)

        # Строим маршрут (прямая линия между pickup и dropoff)
        wps = self._build_route(vehicle.home, mission.pickup, mission.dropoff)
        await self.missions_repo.save_waypoints(mission.id, wps)
        await self.missions_repo.set_status(mission.id, MissionStatus.PLANNED)

        log.info(f"Mission {mission.id} planned with {len(wps)} waypoints for {vehicle.id}")
        return wps

    def _build_route(self, home: LLA, pickup: LLA, drop: LLA):
        """Формирует стандартный маршрут home → pickup → drop → home_rtl."""
        return [
            Waypoint(kind="home", pos=home, order=0),
            Waypoint(kind="pickup", pos=pickup, order=1),
            Waypoint(kind="drop", pos=drop, order=2),
            Waypoint(kind="home_rtl", pos=home, order=3),
        ]


async def _test():
    planner = Planner()
    from drone_core.domain.models import Mission, LLA, Vehicle
    fleet, missions = planner.fleet_repo, planner.missions_repo

    # ✅ добавляем хотя бы один дрон
    v = Vehicle(name="sim-veh-1", home=LLA(lat=52.0, lon=21.0, alt=30))
    await fleet.add(v)

    # 🛰 создаём миссию
    m = Mission(pickup=LLA(lat=52.1, lon=21.1), dropoff=LLA(lat=52.2, lon=21.2))
    await missions.create(m)

    # 🚀 запускаем планирование маршрута
    wps = await planner.plan_mission(m.id)
    if not wps:
        print("❌ Нет свободных дронов — маршрут не построен")
        return

    print(f"✅ Маршрут для миссии {m.id}:")
    for wp in wps:
        print(f" - {wp.kind}: {wp.pos.lat:.4f}, {wp.pos.lon:.4f}")

if __name__ == "__main__":
    asyncio.run(_test())
