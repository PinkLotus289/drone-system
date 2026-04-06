from __future__ import annotations
import asyncio
import json
import logging
from typing import Dict, Optional

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
    - шлёт команды: mission.upload -> arm -> mission.start
    """

    def __init__(self) -> None:
        self.settings = Settings()
        self.fleet, self.missions = make_repos()
        self.bus = MqttBus(self.settings.MQTT_URL)
        self._started = False
        self.loop = asyncio.get_event_loop()
        self._upload_waiters: Dict[str, asyncio.Future[str]] = {}

    # ---- выбор борта ----
    async def _select_vehicle(self) -> Optional[str]:
        allv = await self.fleet.list_all()
        free = [v for v in allv if v.status == VehicleStatus.IDLE and (v.soc or 100) > 40]
        free.sort(key=lambda v: (v.soc or 0), reverse=True)
        return free[0].id if free else None

    # ---- обработчик заказа ----
    async def _on_order_new(self, msg_payload: dict) -> None:
        print("🟢 [ORCH][ORDER] Получен заказ через MQTT")
        log.info(f"[ORCH][ORDER] 📦 Получен новый заказ: {msg_payload}")
        flow_state = "idle"

        def _set_flow_state(new_state: str, reason: str = "") -> None:
            nonlocal flow_state
            if flow_state == new_state:
                return
            suffix = f" ({reason})" if reason else ""
            print(f"🟣 [ORCH][STATE] {flow_state} -> {new_state}{suffix}")
            flow_state = new_state

        try:
            order = Order(**msg_payload)
            print(f"🟢 [ORCH] ✅ Order создан: {order.id}")
        except Exception as e:
            print(f"🔴 [ORCH][ORDER] Ошибка парсинга заказа: {e}")
            _set_flow_state("error", reason="invalid order payload")
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
            _set_flow_state("error", reason="no available vehicle")
            return
        vehicle_id = veh_id if str(veh_id).startswith("veh_") else f"veh_{veh_id}"
        print(f"🟢 [ORCH] 🚁 Назначен дрон: {vehicle_id}")

        await self.missions.assign_vehicle(mission.id, veh_id)
        await self.missions.set_status(mission.id, MissionStatus.ASSIGNED)
        await self._publish(f"mission/{mission.id}/assigned", {"mission_id": mission.id, "vehicle_id": veh_id})
        print("🟢 [ORCH] MQTT → mission/assigned отправлена")

        # === Этап 3: Загрузка маршрута ===
        upload_waiter = asyncio.get_running_loop().create_future()
        self._upload_waiters[mission.id] = upload_waiter
        route_payload = {
            "mission_id": mission.id,
            "waypoints": [w.model_dump() for w in mission.waypoints],
        }
        await self._publish(topics.cmd(vehicle_id, "mission.upload"), route_payload)
        print(f"🟣 [ORCH] [MISSION] upload begin mission_id={mission.id}")
        print(f"🟢 [ORCH] MQTT → cmd/{vehicle_id}/mission.upload отправлена: {len(mission.waypoints)} точек")

        print(f"🟡 [ORCH] Ожидаю подтверждение загрузки от bridge: mission/{mission.id}/status")
        try:
            upload_status = await asyncio.wait_for(upload_waiter, timeout=30.0)
        except asyncio.TimeoutError:
            if mission.id in self._upload_waiters:
                del self._upload_waiters[mission.id]
            await self.missions.set_status(mission.id, MissionStatus.ABORTED)
            await self._publish(
                f"mission/{mission.id}/status",
                {"mission_id": mission.id, "status": MissionStatus.ABORTED, "reason": "upload confirmation timeout"},
            )
            print(f"🔴 [ORCH] ⏱️ Таймаут ожидания UPLOADED для mission_id={mission.id}")
            print(f"🔴 [ORCH] [MISSION] upload result=UPLOAD_FAILED mission_id={mission.id} reason=timeout")
            _set_flow_state("error", reason="upload confirmation timeout")
            return
        finally:
            if mission.id in self._upload_waiters:
                del self._upload_waiters[mission.id]

        if upload_status != "UPLOADED":
            await self.missions.set_status(mission.id, MissionStatus.ABORTED)
            await self._publish(
                f"mission/{mission.id}/status",
                {"mission_id": mission.id, "status": MissionStatus.ABORTED, "reason": f"upload status={upload_status}"},
            )
            print(f"🔴 [ORCH] ❌ Загрузка миссии не подтверждена bridge: mission_id={mission.id}, status={upload_status}")
            print(f"🔴 [ORCH] [MISSION] upload result={upload_status} mission_id={mission.id}")
            _set_flow_state("error", reason=f"upload status={upload_status}")
            return

        await self.missions.set_status(mission.id, MissionStatus.UPLOADED)
        print(f"🟢 [ORCH] [MISSION] upload result=UPLOADED mission_id={mission.id}")
        _set_flow_state("mission_uploaded")
        print(f"🟢 [ORCH] Подтверждён upload от bridge: mission_id={mission.id}, status=UPLOADED")

        # === Этап 4: Старт миссии через PX4 mission flow ===
        print(f"🟡 [ORCH] Запускаю нативный поток PX4: arm -> mission.start (mission_id={mission.id})")
        _set_flow_state("arming")
        await self._publish(topics.cmd(vehicle_id, "arm"), {"mission_id": mission.id})
        print(f"🟢 [ORCH] MQTT → cmd/{vehicle_id}/arm отправлена")
        _set_flow_state("armed")

        await asyncio.sleep(0.5)
        print(f"🟣 [ORCH] [MISSION] start begin mission_id={mission.id}")
        await self._publish(topics.cmd(vehicle_id, "mission.start"), {"mission_id": mission.id})
        print(f"🟢 [ORCH] MQTT → cmd/{vehicle_id}/mission.start отправлена")
        print(f"🟢 [ORCH] [MISSION] start result=STARTED mission_id={mission.id}")
        _set_flow_state("mission_running")

        await self.missions.set_status(mission.id, MissionStatus.IN_PROGRESS)
        await self._publish(f"mission/{mission.id}/status",
                            {"mission_id": mission.id, "status": MissionStatus.IN_PROGRESS})
        print(f"🟢 [ORCH] Статус миссии: IN_PROGRESS (управление маршрутом передано PX4, mission_id={mission.id})")

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
                if message.topic != "orders/new":
                    return
                payload = message.payload
                if isinstance(payload, (bytes, bytearray)):
                    payload = json.loads(payload.decode("utf-8"))
                elif isinstance(payload, str):
                    payload = json.loads(payload)
                elif not isinstance(payload, dict):
                    log.warning("[ORCH][ORDER] Пропуск non-dict payload в orders/new: topic=%s", message.topic)
                    return
                asyncio.run_coroutine_threadsafe(
                    self._on_order_new(payload), self.loop
                )
            except Exception as e:
                log.exception("[ORCH][ORDER] Ошибка в обработчике orders/new: %s", e)

        self.bus.subscribe("orders/new", _handler, qos=1)

        # === Подписка на подтверждения mission upload от bridge ===
        def _mission_status_handler(message):
            try:
                if not (message.topic.startswith("mission/") and message.topic.endswith("/status")):
                    return
                payload = message.payload
                if isinstance(payload, (bytes, bytearray)):
                    payload = json.loads(payload.decode("utf-8"))
                elif isinstance(payload, str):
                    payload = json.loads(payload)
                elif not isinstance(payload, dict):
                    return

                mission_id = str(payload.get("mission_id") or "")
                status = str(payload.get("status") or "")
                if not mission_id or status not in ("UPLOADED", "UPLOAD_FAILED"):
                    return

                print(f"🟣 [ORCH][MISSION] upload result={status} mission_id={mission_id}")

                waiter = self._upload_waiters.get(mission_id)
                if waiter and not waiter.done():
                    self.loop.call_soon_threadsafe(waiter.set_result, status)
                    print(f"🟡 [ORCH][MISSION] Получен upload status от bridge: mission_id={mission_id}, status={status}")
            except Exception as e:
                log.error(f"[ORCH][MISSION] Ошибка обработки mission status: {e}")

        self.bus.subscribe("mission/+/status", _mission_status_handler, qos=1)

        # === 🔥 ДОБАВЬ ЭТО: Подписка на fleet/active ===
        def _fleet_handler(message):
            try:
                if message.topic != "fleet/active":
                    return
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
                raw_status = str(payload.get("status", "IDLE"))
                try:
                    vehicle_status = VehicleStatus(raw_status)
                except ValueError:
                    log.warning(
                        "[ORCH][STATE] Пропуск fleet/active с невалидным VehicleStatus='%s' (id=%s)",
                        raw_status,
                        veh_id,
                    )
                    return

                vehicle = Vehicle(
                    id=veh_id,
                    name=payload.get("name", veh_id),
                    status=vehicle_status,
                    pos=LLA(
                        lat=float(payload.get("lat") or 0),
                        lon=float(payload.get("lon") or 0),
                        alt=float(payload.get("alt") or 0),
                    ),
                    soc=float(payload.get("soc") or 100.0),
                )

                # асинхронно добавляем в локальный FleetMem
                asyncio.run_coroutine_threadsafe(self.fleet.add(vehicle), self.loop)
                log.info(f"🛰️ [ORCH][STATE] Fleet обновлён: {vehicle.id} ({vehicle.status})")

            except Exception as e:
                log.error(f"[ORCH][STATE] Ошибка обработки fleet/active: {e}")

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
