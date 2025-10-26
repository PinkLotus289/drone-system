from __future__ import annotations
import asyncio
import json
import logging
from typing import Optional

from drone_core.config.settings import Settings
from drone_core.infra.repositories import make_repos
from drone_core.domain.models import Order, MissionStatus, VehicleStatus
from drone_core.workers.planner import plan_order
from drone_core.infra.messaging.mqtt_bus import MqttBus
from drone_core.infra.messaging import topics  # твой topics.py

log = logging.getLogger("orchestrator")


class Orchestrator:
    """
    MVP-оркестратор:
    - слушает orders/new
    - планирует миссию (base -> addr1 -> addr2 -> base)
    - выбирает свободный борт по SoC/статусу
    - шлёт команды: mission.upload -> arm -> takeoff -> goto... -> land
    """

    def __init__(self) -> None:
        self.settings = Settings()
        self.fleet, self.missions = make_repos()
        self.bus = MqttBus(self.settings.MQTT_URL)
        self._started = False

    # ---- выбор борта ----
    async def _select_vehicle(self) -> Optional[str]:
        allv = await self.fleet.list_all()
        free = [v for v in allv if v.status == VehicleStatus.IDLE and (v.soc or 100) > 40]
        free.sort(key=lambda v: (v.soc or 0), reverse=True)
        return free[0].id if free else None

    # ---- обработчик заказа ----
    async def _on_order_new(self, msg_payload: dict) -> None:
        order = Order(**msg_payload)
        mission = plan_order(order)
        mission = await self.missions.create(mission)

        # уведомим о планировании
        await self._publish(f"mission/{mission.id}/planned", mission.dict())

        veh_id = await self._select_vehicle()
        if not veh_id:
            log.warning("Нет свободных бортов — миссия остаётся PLANNED")
            return

        await self.missions.assign_vehicle(mission.id, veh_id)
        await self.missions.set_status(mission.id, MissionStatus.ASSIGNED)

        await self._publish(f"mission/{mission.id}/assigned",
                            {"mission_id": mission.id, "vehicle_id": veh_id})

        # выгрузим маршрут в борт
        await self._publish(topics.cmd(veh_id, "mission.upload"), {
            "mission_id": mission.id,
            "waypoints": [w.dict() for w in mission.waypoints],
        })

        # (MVP) мини-пауза вместо подтверждения
        await asyncio.sleep(1.0)
        await self.missions.set_status(mission.id, MissionStatus.UPLOADED)
        await self._publish(f"mission/{mission.id}/status",
                            {"mission_id": mission.id, "status": MissionStatus.UPLOADED})

        # взлёт
        await self._publish(topics.cmd(veh_id, "arm"), {"mission_id": mission.id})
        await asyncio.sleep(0.2)
        await self._publish(topics.cmd(veh_id, "takeoff"),
                            {"mission_id": mission.id, "alt": mission.waypoints[0].pos.alt})

        await self.missions.set_status(mission.id, MissionStatus.IN_PROGRESS)
        await self._publish(f"mission/{mission.id}/status",
                            {"mission_id": mission.id, "status": MissionStatus.IN_PROGRESS})

        # NAV точки
        for wp in mission.waypoints:
            if wp.kind == "NAV":
                await self._publish(topics.cmd(veh_id, "goto"), {
                    "mission_id": mission.id,
                    "lat": wp.pos.lat, "lon": wp.pos.lon, "alt": wp.pos.alt
                })
                await asyncio.sleep(max(wp.hold_s, 1.0))

        # посадка
        await self._publish(topics.cmd(veh_id, "land"), {"mission_id": mission.id})
        await asyncio.sleep(2.0)

        await self.missions.set_status(mission.id, MissionStatus.COMPLETED)
        await self._publish(f"mission/{mission.id}/status",
                            {"mission_id": mission.id, "status": MissionStatus.COMPLETED})

    async def _publish(self, topic: str, payload: dict) -> None:
        # MqttBus.publish синхронный — завернём в default loop
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.bus.publish, topic, payload, 1, False)

    # ---- запуск/подписка ----
    def start(self) -> None:
        if self._started:
            return
        self.bus.start()
        # подписка на заказы
        def _handler(message):
            try:
                payload = message.payload
                if isinstance(payload, (bytes, bytearray)):
                    payload = json.loads(payload.decode("utf-8"))
                elif isinstance(payload, str):
                    payload = json.loads(payload)
                asyncio.run_coroutine_threadsafe(self._on_order_new(payload), asyncio.get_event_loop())
            except Exception as e:
                log.exception("order/new handler error: %s", e)

        self.bus.subscribe("orders/new", _handler, qos=1)
        self._started = True


async def main():
    orch = Orchestrator()
    # отдельная петля под MqttBus уже крутится в его потоках; здесь просто ждём
    orch.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())
