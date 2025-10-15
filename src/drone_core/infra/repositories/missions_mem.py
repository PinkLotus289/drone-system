from __future__ import annotations
import asyncio
from typing import Dict, List, Optional
from drone_core.domain.models import Mission, MissionStatus, Waypoint
from .base import MissionRepo

class MissionsMem(MissionRepo):
    def __init__(self) -> None:
        self._store: Dict[str, Mission] = {}
        self._lock = asyncio.Lock()

    async def create(self, m: Mission) -> Mission:
        async with self._lock:
            self._store[m.id] = m
        return m

    async def get(self, mission_id: str) -> Optional[Mission]:
        return self._store.get(mission_id)

    async def set_status(self, mission_id: str, status: MissionStatus) -> None:
        async with self._lock:
            if mission_id in self._store:
                self._store[mission_id].status = status

    async def assign_vehicle(self, mission_id: str, vehicle_id: str) -> None:
        async with self._lock:
            if mission_id in self._store:
                self._store[mission_id].vehicle_id = vehicle_id

    async def save_waypoints(self, mission_id: str, wps: List[Waypoint]) -> None:
        async with self._lock:
            if mission_id in self._store:
                self._store[mission_id].waypoints = wps

    async def list_active(self) -> List[Mission]:
        return [m for m in self._store.values() if m.status not in
                {MissionStatus.COMPLETED, MissionStatus.ABORTED}]
