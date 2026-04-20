"""SimulatorBackend — реализация DroneBackend поверх PX4 SITL.

Переиспользует существующий код из src/simulator/:
- px4_launcher.start_px4_instances() для запуска PX4
- mavsdk_bridge.connect_system() для подключения MAVSDK
- mavsdk_bridge.run_for_drone() для телеметрии и команд
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import AsyncIterator

from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan

from drone_core.domain.drone_backend import DroneBackend
from drone_core.domain.models import LLA, Waypoint
from simulator.px4_launcher import start_px4_instances
from simulator.mavsdk_bridge import connect_system, run_for_drone

log = logging.getLogger("simulator-backend")


def _waypoint_to_mission_item(wp: Waypoint) -> MissionItem:
    """Конвертирует доменный Waypoint в MAVSDK MissionItem."""
    kind = (wp.kind or "NAV").upper()
    action_map = {
        "NAV": MissionItem.VehicleAction.NONE,
        "TAKEOFF": MissionItem.VehicleAction.TAKEOFF,
        "LAND": MissionItem.VehicleAction.LAND,
    }
    vehicle_action = action_map.get(kind, MissionItem.VehicleAction.NONE)

    return MissionItem(
        latitude_deg=wp.pos.lat,
        longitude_deg=wp.pos.lon,
        relative_altitude_m=wp.pos.alt,
        speed_m_s=5.0,
        is_fly_through=True,
        gimbal_pitch_deg=0.0,
        gimbal_yaw_deg=0.0,
        camera_action=MissionItem.CameraAction.NONE,
        loiter_time_s=wp.hold_s,
        camera_photo_interval_s=0.0,
        acceptance_radius_m=5.0,
        yaw_deg=0.0,
        camera_photo_distance_m=0.0,
        vehicle_action=vehicle_action,
    )


class SimulatorBackend(DroneBackend):
    """PX4 SITL бэкенд для режима test."""

    def __init__(self) -> None:
        # drone_id → MAVSDK System
        self._systems: dict[str, System] = {}
        # PX4 subprocess-ы
        self._px4_procs: list = []
        # asyncio tasks для bridge
        self._bridge_tasks: list[asyncio.Task] = []
        # конфиг симулятора
        self._config: dict = {}

    async def start(self, config: dict) -> None:
        """Запуск PX4 SITL инстансов и подключение через MAVSDK."""
        self._config = config
        drones = config.get("drones", [])
        sim_home = config.get("simulator", {}).get("home", {})
        home_lat = sim_home.get("lat", 0.0)
        home_lon = sim_home.get("lon", 0.0)
        home_alt = sim_home.get("alt", 0.0)

        # 1. Запуск PX4 инстансов
        log.info("Запуск PX4 SITL инстансов...")
        self._px4_procs = await start_px4_instances(config)

        # 2. Подключение MAVSDK к каждому дрону
        for d in drones:
            instance_id = str(d["id"])
            drone_id = f"veh_{instance_id}"
            connection_url = f"udp://:{d['mavlink_out']}"

            log.info("Подключение MAVSDK к %s через %s", drone_id, connection_url)
            system = await connect_system(connection_url)
            self._systems[drone_id] = system

        log.info("SimulatorBackend: подключено %d дронов", len(self._systems))

        # 3. Запуск bridge-задач (телеметрия + обработка команд через MQTT)
        for d in drones:
            instance_id = str(d["id"])
            task = asyncio.create_task(
                run_for_drone(
                    instance_id=instance_id,
                    connection_url=f"udp://:{d['mavlink_out']}",
                    home_lat=home_lat,
                    home_lon=home_lon,
                    home_alt=home_alt,
                )
            )
            self._bridge_tasks.append(task)

    async def stop(self) -> None:
        """Остановка PX4 процессов и MAVSDK-соединений."""
        # Отменяем bridge-задачи
        for task in self._bridge_tasks:
            task.cancel()
        if self._bridge_tasks:
            await asyncio.gather(*self._bridge_tasks, return_exceptions=True)
        self._bridge_tasks.clear()

        # Завершаем PX4 процессы
        for p in self._px4_procs:
            if p and p.poll() is None:
                p.terminate()
        await asyncio.sleep(2)
        for p in self._px4_procs:
            if p and p.poll() is None:
                p.kill()
        self._px4_procs.clear()

        # Убиваем оставшиеся PX4 процессы
        os.system("pkill -f px4 > /dev/null 2>&1")

        self._systems.clear()
        log.info("SimulatorBackend остановлен.")

    async def get_connected_drones(self) -> list[str]:
        return list(self._systems.keys())

    def _get_system(self, drone_id: str) -> System:
        sys = self._systems.get(drone_id)
        if sys is None:
            raise ValueError(f"Дрон {drone_id} не подключён")
        return sys

    async def arm(self, drone_id: str) -> None:
        sys = self._get_system(drone_id)
        await sys.action.arm()
        log.info("[%s] Armed", drone_id)

    async def disarm(self, drone_id: str) -> None:
        sys = self._get_system(drone_id)
        await sys.action.disarm()
        log.info("[%s] Disarmed", drone_id)

    async def takeoff(self, drone_id: str, altitude_m: float) -> None:
        sys = self._get_system(drone_id)
        await sys.action.set_takeoff_altitude(altitude_m)
        await sys.action.takeoff()
        log.info("[%s] Takeoff → %.1f m", drone_id, altitude_m)

    async def land(self, drone_id: str) -> None:
        sys = self._get_system(drone_id)
        await sys.action.land()
        log.info("[%s] Landing", drone_id)

    async def go_to(self, drone_id: str, target: LLA) -> None:
        sys = self._get_system(drone_id)
        await sys.action.goto_location(target.lat, target.lon, target.alt, 0)
        log.info("[%s] GoTo → (%.6f, %.6f, %.1f)", drone_id, target.lat, target.lon, target.alt)

    async def upload_mission(self, drone_id: str, waypoints: list[Waypoint]) -> None:
        sys = self._get_system(drone_id)
        mission_items = [_waypoint_to_mission_item(wp) for wp in waypoints]
        plan = MissionPlan(mission_items)
        await sys.mission.upload_mission(plan)
        log.info("[%s] Mission uploaded (%d waypoints)", drone_id, len(mission_items))

    async def start_mission(self, drone_id: str) -> None:
        sys = self._get_system(drone_id)
        await sys.mission.start_mission()
        log.info("[%s] Mission started", drone_id)

    async def telemetry_stream(self, drone_id: str) -> AsyncIterator[dict]:
        sys = self._get_system(drone_id)
        async for pos in sys.telemetry.position():
            yield {
                "drone_id": drone_id,
                "lat": pos.latitude_deg,
                "lon": pos.longitude_deg,
                "alt": pos.relative_altitude_m,
                "ts": time.time(),
            }
            await asyncio.sleep(1.0)