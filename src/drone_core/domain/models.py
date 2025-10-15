from __future__ import annotations
from enum import Enum
from typing import Optional, List, Literal
from datetime import datetime
from pydantic import BaseModel, Field
from uuid import uuid4


class VehicleStatus(str, Enum):
    IDLE = "IDLE"
    BUSY = "BUSY"
    OFFLINE = "OFFLINE"
    ERROR = "ERROR"


class MissionStatus(str, Enum):
    CREATED = "CREATED"
    PLANNED = "PLANNED"
    ARMING = "ARMING"
    TAKEOFF = "TAKEOFF"
    ENROUTE_TO_A = "ENROUTE_TO_A"
    AT_PICKUP = "AT_PICKUP"
    PICKUP_DONE = "PICKUP_DONE"
    ENROUTE_TO_B = "ENROUTE_TO_B"
    AT_DROPOFF = "AT_DROPOFF"
    DROPOFF_DONE = "DROPOFF_DONE"
    RTL = "RTL"
    LANDED = "LANDED"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class LLA(BaseModel):
    """Координаты точки в формате lat/lon/alt"""
    lat: float
    lon: float
    alt: float = 30.0


class Waypoint(BaseModel):
    """Точка маршрута"""
    kind: Literal["home", "pickup", "drop", "home_rtl"] = "home"
    pos: LLA
    hold_sec: float = 0.0
    order: int = 0


class Vehicle(BaseModel):
    """Описание дрона/симулятора"""
    id: str = Field(default_factory=lambda: f"veh_{uuid4().hex[:8]}")
    name: str = "sim-vehicle"
    max_payload_kg: float = 5.0
    home: LLA = Field(default_factory=lambda: LLA(lat=52.0, lon=21.0, alt=30.0))
    status: VehicleStatus = VehicleStatus.IDLE
    last_seen_ts: Optional[float] = None


class Mission(BaseModel):
    """Миссия доставки"""
    id: str = Field(default_factory=lambda: f"mis_{uuid4().hex[:8]}")
    pickup: LLA
    dropoff: LLA
    payload_kg: float = 2.0
    priority: Literal["low", "normal", "high"] = "normal"

    vehicle_id: Optional[str] = None
    status: MissionStatus = MissionStatus.CREATED
    waypoints: List[Waypoint] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=datetime.utcnow)
