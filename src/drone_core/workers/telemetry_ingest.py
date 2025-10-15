import asyncio
import json
import logging
import sys
from pathlib import Path

# --- гарантируем доступ к пакету src ---
sys.path.append(str(Path(__file__).resolve().parents[2]))

from drone_core.infra.messaging.mqtt_bus import MqttBus
from drone_core.infra.messaging.topics import TelemetryTopics
from drone_core.infra.repositories import make_repos

logger = logging.getLogger("telemetry-ingest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

# создаём репозитории (FleetMem или FleetPg — выберется автоматически)
fleet_repo, _ = make_repos()


async def handle_message(topic: str, payload: str):
    """Обработка входящей телеметрии."""
    try:
        parts = topic.split("/")
        if len(parts) < 3:
            logger.warning(f"Некорректный топик: {topic}")
            return

        _, veh_id, telem_type = parts
        data = json.loads(payload)

        if telem_type == "pose":
            await fleet_repo.update_pose(veh_id, data)
            logger.info(f"<< pose {veh_id}: {data}")
        elif telem_type == "battery":
            await fleet_repo.update_battery(veh_id, data)
            logger.info(f"<< battery {veh_id}: {data}")
        elif telem_type == "health":
            await fleet_repo.update_health(veh_id, data)
            logger.info(f"<< health {veh_id}: {data}")
        else:
            logger.debug(f"Неизвестный тип телеметрии {telem_type}: {data}")

    except Exception as e:
        logger.error(f"Ошибка обработки телеметрии: {e}")


async def main():
    logger.info("Telemetry Ingest запускается...")
    bus = MqttBus(client_id="telemetry-ingest")
    await bus.connect()
    await bus.subscribe(TelemetryTopics.ALL, handle_message)
    logger.info(f"Подписан на {TelemetryTopics.ALL}")
    await asyncio.Future()  # keep alive


if __name__ == "__main__":
    asyncio.run(main())
