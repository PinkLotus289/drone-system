from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from drone_core.domain.models import LLA, Waypoint


class DroneBackend(ABC):
    """Абстракция над способом подключения и управления дронами.

    Реализации:
    - SimulatorBackend  (test)     — PX4 SITL
    - PreflightBackend  (preflight) — реальный дрон, простые операции
    - FullBackend       (full)      — полноценное управление реальными дронами
    """

    @abstractmethod
    async def start(self, config: dict) -> None:
        """Инициализация бэкенда (запуск PX4, подключение к железу и т.д.)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Корректное завершение всех подключений и процессов."""
        ...

    @abstractmethod
    async def get_connected_drones(self) -> list[str]:
        """Возвращает список ID подключённых дронов."""
        ...

    @abstractmethod
    async def arm(self, drone_id: str) -> None:
        ...

    @abstractmethod
    async def disarm(self, drone_id: str) -> None:
        ...

    @abstractmethod
    async def takeoff(self, drone_id: str, altitude_m: float) -> None:
        ...

    @abstractmethod
    async def land(self, drone_id: str) -> None:
        ...

    @abstractmethod
    async def go_to(self, drone_id: str, target: LLA) -> None:
        """Отправить дрон в указанную точку."""
        ...

    @abstractmethod
    async def upload_mission(self, drone_id: str, waypoints: list[Waypoint]) -> None:
        """Загрузить миссию (список вейпоинтов) на дрон."""
        ...

    @abstractmethod
    async def start_mission(self, drone_id: str) -> None:
        """Запустить выполнение загруженной миссии."""
        ...

    @abstractmethod
    async def telemetry_stream(self, drone_id: str) -> AsyncIterator[dict]:
        """Асинхронный генератор телеметрии для конкретного дрона."""
        ...
