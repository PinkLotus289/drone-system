from __future__ import annotations
from typing import List, Optional
from sqlmodel import SQLModel, Field, Relationship, select
from drone_core.domain.models import Mission, MissionStatus, Waypoint, LLA
from drone_core.infra.db.postgres import session
from .base import MissionRepo

class MissionRow(SQLModel, table=True):
    id: str = Field(primary_key=True)
    pickup_lat: float
    pickup_lon: float
    pickup_alt: float
    drop_lat: float
    drop_lon: float
    drop_alt: float
    payload_kg: float
    priority: str
    vehicle_id: str | None = None
    status: str
    created_at: str
    waypoints: list["WaypointRow"] = Relationship(back_populates="mission")

class WaypointRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    mission_id: str = Field(foreign_key="missionrow.id")
    kind: str
    order: int
    lat: float
    lon: float
    alt: float
    hold_sec: float
    mission: MissionRow | None = Relationship(back_populates="waypoints")

def _to_domain(m: MissionRow, wps: List[WaypointRow]) -> Mission:
    return Mission(
        id=m.id,
        pickup=LLA(lat=m.pickup_lat, lon=m.pickup_lon, alt=m.pickup_alt),
        dropoff=LLA(lat=m.drop_lat, lon=m.drop_lon, alt=m.drop_alt),
        payload_kg=m.payload_kg,
        priority=m.priority,  # type: ignore
        vehicle_id=m.vehicle_id,
        status=MissionStatus(m.status),
        waypoints=[Waypoint(kind=w.kind, order=w.order, pos=LLA(lat=w.lat, lon=w.lon, alt=w.alt), hold_sec=w.hold_sec) for w in sorted(wps, key=lambda x: x.order)],
        created_at=m.created_at,  # str/iso — как у тебя в домене
    )

class MissionsPg(MissionRepo):
    async def create(self, m: Mission) -> Mission:
        mr = MissionRow(
            id=m.id,
            pickup_lat=m.pickup.lat, pickup_lon=m.pickup.lon, pickup_alt=m.pickup.alt,
            drop_lat=m.dropoff.lat, drop_lon=m.dropoff.lon, drop_alt=m.dropoff.alt,
            payload_kg=m.payload_kg, priority=m.priority,
            vehicle_id=m.vehicle_id, status=m.status.value,
            created_at=m.created_at.isoformat(),
        )
        async with session() as s:
            s.add(mr)
            await s.commit()
        return m

    async def get(self, mission_id: str) -> Optional[Mission]:
        async with session() as s:
            res = await s.exec(select(MissionRow).where(MissionRow.id == mission_id))
            mr = res.one_or_none()
            if not mr:
                return None
            res_wp = await s.exec(select(WaypointRow).where(WaypointRow.mission_id == mission_id))
            return _to_domain(mr, res_wp.all())

    async def set_status(self, mission_id: str, status: MissionStatus) -> None:
        async with session() as s:
            res = await s.exec(select(MissionRow).where(MissionRow.id == mission_id))
            mr = res.one_or_none()
            if mr:
                mr.status = status.value
                s.add(mr)
                await s.commit()

    async def assign_vehicle(self, mission_id: str, vehicle_id: str) -> None:
        async with session() as s:
            res = await s.exec(select(MissionRow).where(MissionRow.id == mission_id))
            mr = res.one_or_none()
            if mr:
                mr.vehicle_id = vehicle_id
                s.add(mr)
                await s.commit()

    async def save_waypoints(self, mission_id: str, wps: List[Waypoint]) -> None:
        async with session() as s:
            # удалим старые и добавим новые (просто и понятно для MVP)
            await s.exec(select(WaypointRow).where(WaypointRow.mission_id == mission_id))
            # У SQLModel нет bulk delete async «из коробки», можно сделать raw SQL, но для MVP — insert поверх (PK autoinc).
            for w in wps:
                s.add(WaypointRow(
                    mission_id=mission_id, kind=w.kind, order=w.order,
                    lat=w.pos.lat, lon=w.pos.lon, alt=w.pos.alt, hold_sec=w.hold_sec
                ))
            await s.commit()

    async def list_active(self) -> List[Mission]:
        async with session() as s:
            res = await s.exec(select(MissionRow).where(MissionRow.status.not_in(
                [MissionStatus.COMPLETED.value, MissionStatus.ABORTED.value]
            )))
            rows = res.all()
            missions: List[Mission] = []
            for r in rows:
                wps = (await s.exec(select(WaypointRow).where(WaypointRow.mission_id == r.id))).all()
                missions.append(_to_domain(r, wps))
            return missions
