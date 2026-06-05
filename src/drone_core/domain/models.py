from __future__ import annotations
from enum import Enum
from typing import Optional, List, Literal
from datetime import datetime
from pydantic import BaseModel, Field
from uuid import uuid4
from pydantic import BaseModel, ConfigDict
from typing import List, Optional


# ---------- Vehicles ----------
class VehicleStatus(str, Enum):
    IDLE = "IDLE"
    BUSY = "BUSY"
    FLYING = "FLYING"
    OFFLINE = "OFFLINE"
    ERROR = "ERROR"


class LLA(BaseModel):
    lat: float
    lon: float
    alt: float = 60.0


class Vehicle(BaseModel):
    id: str
    name: Optional[str] = None
    status: VehicleStatus = VehicleStatus.IDLE
    # простая телеметрия (обновляется ingest'ом)
    pos: Optional[LLA] = None
    soc: Optional[float] = None  # %
    mode: Optional[str] = None
    last_ts: Optional[float] = None


# ---------- Missions / Orders ----------
class MissionStatus(str, Enum):
    CREATED = "CREATED"
    PLANNED = "PLANNED"
    ASSIGNED = "ASSIGNED"
    UPLOADED = "UPLOADED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class Waypoint(BaseModel):
    pos: LLA
    kind: Literal["TAKEOFF", "NAV", "LAND", "RTL", "ORBIT"] = "NAV"
    hold_s: float = 0.0          # для NAV — время зависания (hover) на точке;
                                 # для ORBIT — длительность кружения над точкой
    speed_m_s: float = 5.0       # cruise speed для этого waypoint'а
    acceptance_radius_m: float = 5.0  # «достигнут ли» порог для NAV-item
    orbit_radius_m: float = 0.0  # радиус орбиты для kind=ORBIT (ISR)


class Mission(BaseModel):
    """
    Миссия доставки (минимальный контракт для репозиториев и оркестратора).
    Совместима с твоими репозиториями: id/vehicle_id/status/waypoints.
    Оставляю pickup/dropoff для обратной совместимости UI, но фактический маршрут в waypoints.
    """
    id: str = Field(default_factory=lambda: f"mis_{uuid4().hex[:8]}")
    # старые поля (совместимость)
    pickup: Optional[LLA] = None
    dropoff: Optional[LLA] = None

    # полезная нагрузка/приоритет
    payload_kg: float = 2.0
    priority: Literal["low", "normal", "high", "urgent"] = "normal"

    # исполнение
    vehicle_id: Optional[str] = None
    status: MissionStatus = MissionStatus.CREATED
    mission_type: str = "delivery"  # delivery | isr | patrol | sector — для UI/раскраски
    notes: Optional[str] = None     # операторские пометки из формы new mission (для UI)
    takeoff_profile: str = "vertical"  # vertical | inclined — для отрисовки lead-in на карте
    waypoints: List[Waypoint] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = ConfigDict(
        ser_json_timedelta="iso8601",
        ser_json_datetime="iso8601"
    )


class Order(BaseModel):
    """
    Заказ уровня UI: база + два адреса.
    """
    id: str = Field(default_factory=lambda: f"ord_{uuid4().hex[:8]}")
    base: LLA
    addr1: LLA
    addr2: LLA
    payload_kg: float = 2.0
    priority: Literal["low", "normal", "high", "urgent"] = "normal"

    model_config = ConfigDict(
        ser_json_timedelta="iso8601",
        ser_json_datetime="iso8601"
    )
