from __future__ import annotations
import asyncio
from typing import Dict, List, Optional
from drone_core.domain.models import Vehicle, VehicleStatus
from .base import VehicleRepo

class FleetMem(VehicleRepo):
    def __init__(self) -> None:
        self._store: Dict[str, Vehicle] = {}
        self._lock = asyncio.Lock()

    async def add(self, v: Vehicle) -> Vehicle:
        async with self._lock:
            self._store[v.id] = v
        return v

    async def get(self, vehicle_id: str) -> Optional[Vehicle]:
        return self._store.get(vehicle_id)

    async def list_all(self) -> List[Vehicle]:
        return list(self._store.values())

    async def list_free(self) -> List[Vehicle]:
        return [v for v in self._store.values() if v.status == VehicleStatus.IDLE]

    async def set_status(self, vehicle_id: str, status: VehicleStatus) -> None:
        async with self._lock:
            if vehicle_id in self._store:
                self._store[vehicle_id].status = status

    async def update(self, v: Vehicle) -> None:
        async with self._lock:
            self._store[v.id] = v
