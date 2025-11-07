import json
import logging
import sys
from pathlib import Path
import asyncio
import time

# гарантируем доступ к src/
sys.path.append(str(Path(__file__).resolve().parents[2]))

from drone_core.config.settings import Settings
from drone_core.infra.messaging.mqtt_bus import MqttBus
from drone_core.infra.messaging.topics import TelemetryTopics
from drone_core.infra.repositories import make_repos
from drone_core.infra.messaging.bus import Message  # тип сообщения от MQTT
from drone_core.domain.models import LLA, Vehicle, VehicleStatus

logger = logging.getLogger("telemetry-ingest")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

fleet_repo, _ = make_repos()
LAST_TELEM = {}


# --- обработка телеметрии ---
def handle_message(msg: Message):
    """Обработка входящих сообщений от MQTT."""
    try:
        #logger.info(f"📡 Получен MQTT топик: {msg.topic}")
        topic = msg.topic
        payload = msg.payload

        parts = topic.split("/")
        if len(parts) < 3:
            return  # не телеметрический топик

        _, veh_id, telem_type = parts
        if isinstance(payload, (bytes, bytearray)):
            try:
                payload = json.loads(payload.decode("utf-8"))
            except Exception:
                pass
        elif isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                pass

        d = LAST_TELEM.setdefault(veh_id, {})
        d[telem_type] = payload

    except Exception as e:
        logger.exception(f"Ошибка обработки телеметрии: {e}")


# --- обработка fleet/active ---
def handle_fleet_active(msg: Message):
    """Добавляем/обновляем дронов в репозитории при получении fleet/active."""
    try:
        # фильтрация по точному топику
        if not msg.topic.endswith("fleet/active"):
            return

        # нормализуем payload
        payload = msg.payload
        try:
            if isinstance(payload, (bytes, bytearray)):
                payload = json.loads(payload.decode("utf-8"))
            elif isinstance(payload, str):
                payload = json.loads(payload)
        except Exception as e:
            logger.error(f"Ошибка парсинга fleet/active payload: {e}")
            return

        if not isinstance(payload, dict):
            logger.warning(f"⚠️ Неожиданный тип payload: {type(payload)}")
            return

        logger.info(f"📦 Получен fleet/active payload: {payload}")

        drone_id = str(payload.get("id"))
        if not drone_id:
            logger.warning("fleet/active без id, игнорирую")
            return

        name = payload.get("name", f"veh_{drone_id}")
        status = payload.get("status", "IDLE")

        # создаём модель дрона
        vehicle = Vehicle(
            id=drone_id,
            name=name,
            status=VehicleStatus(status),
            pos=LLA(
                lat=float(payload.get("lat") or 43.0747),
                lon=float(payload.get("lon") or -89.3842),
                alt=float(payload.get("alt") or 0.0)
            ),
            soc=float(payload.get("soc") or 100.0),
        )

        async def update_repo():
            existing = await fleet_repo.get(drone_id)
            if existing:
                await fleet_repo.update(vehicle)
                logger.info(f"🟡 [INGEST] Обновлён дрон: {name} ({status})")
            else:
                await fleet_repo.add(vehicle)
                logger.info(f"🟢 [INGEST] Добавлен новый дрон: {name} ({status})")

        # отдельный event loop для paho потока
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(update_repo())
        loop.close()

    except Exception as e:
        logger.exception(f"Ошибка обработки fleet/active: {e}")


# --- мониторинг активных дронов ---
async def monitor_fleet():
    """Периодически выводит состав флота в памяти."""
    await asyncio.sleep(5)
    while True:
        allv = await fleet_repo.list_all()
        ids = [v.id for v in allv]
        logger.info(f"🛰️ [MONITOR] Активные дроны в FleetMem: {ids or '— пусто —'}")
        await asyncio.sleep(10)


# --- точка входа ---
def main():
    logger.info("Telemetry Ingest запускается...")

    settings = Settings()
    bus = MqttBus(settings.MQTT_URL, client_id="telemetry-ingest")

    # Подписка
    bus.subscribe(TelemetryTopics.ALL, handle_message)
    bus.subscribe("fleet/active", handle_fleet_active)

    # Запуск MQTT
    print(f"[DEBUG] MQTT URL = {settings.MQTT_URL}")
    print("[DEBUG] Starting MqttBus...")
    bus.start()
    print("[DEBUG] MqttBus started, waiting 3s...")

    # Фоновый мониторинг
    loop = asyncio.get_event_loop()
    loop.create_task(monitor_fleet())

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("Останавливаем Telemetry Ingest...")
        bus.stop()


if __name__ == "__main__":
    main()
