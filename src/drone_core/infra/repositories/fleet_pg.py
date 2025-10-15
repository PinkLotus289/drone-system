from __future__ import annotations
from typing import List, Optional
from datetime import datetime, timezone
from sqlmodel import SQLModel, Field, select
from drone_core.domain.models import Vehicle, VehicleStatus, LLA
from drone_core.infra.db.postgres import session
from .base import VehicleRepo
import logging

logger = logging.getLogger("fleet-pg")


class VehicleRow(SQLModel, table=True):
    """ORM-модель для таблицы fleet (PostgreSQL)."""
    id: str = Field(primary_key=True)
    name: str
    max_payload_kg: float
    home_lat: float
    home_lon: float
    home_alt: float
    status: str
    last_seen_ts: float | None = None
    max_range_km: float | None = None
    speed_mps: float | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def _to_domain(r: VehicleRow) -> Vehicle:
    """Преобразование ORM-объекта в доменную модель."""
    return Vehicle(
        id=r.id,
        name=r.name,
        max_payload_kg=r.max_payload_kg,
        home=LLA(lat=r.home_lat, lon=r.home_lon, alt=r.home_alt),
        status=VehicleStatus(r.status),
        last_seen_ts=r.last_seen_ts,
        max_range_km=r.max_range_km,
        speed_mps=r.speed_mps,
    )


class FleetPg(VehicleRepo):
    """PostgreSQL-реестр дронов (Fleet Registry)."""

    async def add(self, v: Vehicle) -> Vehicle:
        """Добавить или обновить дрон в БД."""
        row = VehicleRow(
            id=v.id,
            name=v.name,
            max_payload_kg=v.max_payload_kg,
            home_lat=v.home.lat,
            home_lon=v.home.lon,
            home_alt=v.home.alt,
            status=v.status.value,
            last_seen_ts=v.last_seen_ts,
            max_range_km=v.max_range_km,
            speed_mps=v.speed_mps,
        )
        async with session() as s:
            s.add(row)
            await s.commit()
            logger.info(f"✅ Added/updated drone {v.name} ({v.id}) with status {v.status}")
        return v

    async def get(self, vehicle_id: str) -> Optional[Vehicle]:
        """Получить дрон по ID."""
        async with session() as s:
            res = await s.exec(select(VehicleRow).where(VehicleRow.id == vehicle_id))
            r = res.one_or_none()
            return _to_domain(r) if r else None

    async def list_all(self) -> List[Vehicle]:
        """Список всех дронов."""
        async with session() as s:
            res = await s.exec(select(VehicleRow))
            return [_to_domain(r) for r in res.all()]

    async def list_free(self) -> List[Vehicle]:
        """Список свободных дронов (FREE)."""
        async with session() as s:
            res = await s.exec(select(VehicleRow).where(VehicleRow.status == VehicleStatus.FREE.value))
            return [_to_domain(r) for r in res.all()]

    async def set_status(self, vehicle_id: str, status: VehicleStatus) -> None:
        """Обновить статус дрона."""
        async with session() as s:
            res = await s.exec(select(VehicleRow).where(VehicleRow.id == vehicle_id))
            r = res.one_or_none()
            if r:
                r.status = status.value
                r.updated_at = datetime.now(timezone.utc)
                s.add(r)
                await s.commit()
                logger.info(f"🔄 Updated drone {r.id} status → {status.value}")

    async def update(self, v: Vehicle) -> None:
        """Обновить все параметры дрона (или добавить, если его нет)."""
        async with session() as s:
            res = await s.exec(select(VehicleRow).where(VehicleRow.id == v.id))
            r = res.one_or_none()
            if not r:
                logger.warning(f"⚠️ Drone {v.id} not found — creating new record.")
                await self.add(v)
                return

            r.name = v.name
            r.max_payload_kg = v.max_payload_kg
            r.home_lat, r.home_lon, r.home_alt = v.home.lat, v.home.lon, v.home.alt
            r.status = v.status.value
            r.last_seen_ts = v.last_seen_ts
            r.max_range_km = v.max_range_km
            r.speed_mps = v.speed_mps
            r.updated_at = datetime.now(timezone.utc)
            s.add(r)
            await s.commit()
            logger.info(f"✅ Updated drone {r.id} parameters.")


# SQL для таблицы fleet:
# CREATE TABLE fleet (
#   id TEXT PRIMARY KEY,
#   name TEXT,
#   max_payload_kg DOUBLE PRECISION,
#   home_lat DOUBLE PRECISION,
#   home_lon DOUBLE PRECISION,
#   home_alt DOUBLE PRECISION,
#   status TEXT,
#   last_seen_ts DOUBLE PRECISION,
#   max_range_km DOUBLE PRECISION,
#   speed_mps DOUBLE PRECISION,
#   updated_at TIMESTAMP WITH TIME ZONE
# );
