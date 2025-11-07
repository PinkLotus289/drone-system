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
        self.loop = asyncio.get_event_loop()

    # ---- выбор борта ----
    async def _select_vehicle(self) -> Optional[str]:
        allv = await self.fleet.list_all()
        free = [v for v in allv if v.status == VehicleStatus.IDLE and (v.soc or 100) > 40]
        free.sort(key=lambda v: (v.soc or 0), reverse=True)
        return free[0].id if free else None

    # ---- обработчик заказа ----
    async def _on_order_new(self, msg_payload: dict) -> None:
        print("🟢 [ORCH] Получен заказ через MQTT")
        log.info(f"📦 Получен новый заказ: {msg_payload}")

        try:
            order = Order(**msg_payload)
            print(f"🟢 [ORCH] ✅ Order создан: {order.id}")
        except Exception as e:
            print(f"🔴 [ORCH] Ошибка парсинга заказа: {e}")
            return

        # === Этап 1: Планирование ===
        mission = plan_order(order)
        print(f"🟢 [ORCH] ✏️ Маршрут построен ({len(mission.waypoints)} точек)")
        mission = await self.missions.create(mission)
        print(f"🟢 [ORCH] 💾 Миссия сохранена в репозитории: {mission.id}")

        print(f"🟡 [ORCH] Пытаюсь опубликовать mission/planned → {mission.id}")
        await self._publish(f"mission/{mission.id}/planned", mission.model_dump())
        print(f"🟢 [ORCH] MQTT → mission/planned опубликована")

        # === Этап 2: Назначение борта ===
        veh_id = await self._select_vehicle()
        if not veh_id:
            print("🔴 [ORCH] ❌ Нет свободных дронов — миссия остаётся PLANNED")
            return
        print(f"🟢 [ORCH] 🚁 Назначен дрон: veh_{veh_id}")

        await self.missions.assign_vehicle(mission.id, veh_id)
        await self.missions.set_status(mission.id, MissionStatus.ASSIGNED)
        await self._publish(f"mission/{mission.id}/assigned", {"mission_id": mission.id, "vehicle_id": veh_id})
        print("🟢 [ORCH] MQTT → mission/assigned отправлена")

        # === Этап 3: Загрузка маршрута ===
        route_payload = {
            "mission_id": mission.id,
            "waypoints": [w.model_dump() for w in mission.waypoints],
        }
        await self._publish(topics.cmd(veh_id, "mission.upload"), route_payload)
        print(f"🟢 [ORCH] MQTT → cmd/veh_{veh_id}/mission.upload отправлена: {len(mission.waypoints)} точек")

        await asyncio.sleep(1.0)
        await self.missions.set_status(mission.id, MissionStatus.UPLOADED)
        await self._publish(f"mission/{mission.id}/status",
                            {"mission_id": mission.id, "status": MissionStatus.UPLOADED})
        print("🟢 [ORCH] MQTT → mission/status: UPLOADED")

        # === Этап 4: Взлёт ===
        await self._publish(topics.cmd(veh_id, "arm"), {"mission_id": mission.id})
        print(f"🟢 [ORCH] MQTT → cmd/veh_{veh_id}/arm отправлена")

        await asyncio.sleep(0.5)
        await self._publish(topics.cmd(veh_id, "takeoff"),
                            {"mission_id": mission.id, "alt": mission.waypoints[0].pos.alt})
        print(f"🟢 [ORCH] MQTT → cmd/veh_{veh_id}/takeoff alt={mission.waypoints[0].pos.alt}")

        await self.missions.set_status(mission.id, MissionStatus.IN_PROGRESS)
        await self._publish(f"mission/{mission.id}/status",
                            {"mission_id": mission.id, "status": MissionStatus.IN_PROGRESS})
        print("🟢 [ORCH] Статус миссии: IN_PROGRESS")

        # === Этап 5: Полёт по точкам ===
        for wp in mission.waypoints:
            if wp.kind == "NAV":
                await self._publish(topics.cmd(veh_id, "goto"), {
                    "mission_id": mission.id,
                    "lat": wp.pos.lat, "lon": wp.pos.lon, "alt": wp.pos.alt
                })
                print(f"🟢 [ORCH] MQTT → cmd/veh_{veh_id}/goto → ({wp.pos.lat:.6f}, {wp.pos.lon:.6f}, alt={wp.pos.alt})")
                await asyncio.sleep(max(wp.hold_s, 1.0))

        # === Этап 6: Завершение миссии ===
        await self._publish(topics.cmd(veh_id, "land"), {"mission_id": mission.id})
        print(f"🟢 [ORCH] MQTT → cmd/veh_{veh_id}/land отправлена")

        await asyncio.sleep(2.0)
        await self.missions.set_status(mission.id, MissionStatus.COMPLETED)
        await self._publish(f"mission/{mission.id}/status",
                            {"mission_id": mission.id, "status": MissionStatus.COMPLETED})
        print(f"🟢 [ORCH] ✅ Миссия {mission.id} завершена")

    async def _publish(self, topic: str, payload: dict) -> None:
        print(f"   [DEBUG PUBLISH] Топик={topic}")
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.bus.publish, topic, payload, 1, False)
            print(f"   [DEBUG PUBLISH] ✔️ Отправлено {topic}")
        except Exception as e:
            print(f"   [DEBUG PUBLISH] ❌ Ошибка при публикации {topic}: {e}")

    # ---- запуск/подписка ----
    def start(self) -> None:
        if self._started:
            return

        self.bus.start()
        log.info("🧭 Orchestrator запущен и слушает заказы...")

        # === Подписка на новые заказы ===
        def _handler(message):
            try:
                payload = message.payload
                if isinstance(payload, (bytes, bytearray)):
                    payload = json.loads(payload.decode("utf-8"))
                elif isinstance(payload, str):
                    payload = json.loads(payload)
                asyncio.run_coroutine_threadsafe(
                    self._on_order_new(payload), self.loop
                )
            except Exception as e:
                log.exception("Ошибка в обработчике order/new: %s", e)

        self.bus.subscribe("orders/new", _handler, qos=1)

        # === 🔥 ДОБАВЬ ЭТО: Подписка на fleet/active ===
        def _fleet_handler(message):
            try:
                payload = message.payload
                if isinstance(payload, (bytes, bytearray)):
                    payload = json.loads(payload.decode("utf-8"))
                elif isinstance(payload, str):
                    payload = json.loads(payload)
                elif not isinstance(payload, dict):
                    return

                veh_id = str(payload.get("id"))
                if not veh_id:
                    return

                from drone_core.domain.models import LLA, Vehicle, VehicleStatus
                vehicle = Vehicle(
                    id=veh_id,
                    name=payload.get("name", veh_id),
                    status=VehicleStatus(payload.get("status", "IDLE")),
                    pos=LLA(
                        lat=float(payload.get("lat") or 0),
                        lon=float(payload.get("lon") or 0),
                        alt=float(payload.get("alt") or 0),
                    ),
                    soc=float(payload.get("soc") or 100.0),
                )

                # асинхронно добавляем в локальный FleetMem
                asyncio.run_coroutine_threadsafe(self.fleet.add(vehicle), self.loop)
                log.info(f"🛰️ [ORCH] Fleet обновлён: {vehicle.id} ({vehicle.status})")

            except Exception as e:
                log.error(f"[ORCH] Ошибка обработки fleet/active: {e}")

        self.bus.subscribe("fleet/active", _fleet_handler, qos=1)
        # === 🔥 конец добавленного блока ===

        self._started = True


async def main():
    orch = Orchestrator()
    orch.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())
