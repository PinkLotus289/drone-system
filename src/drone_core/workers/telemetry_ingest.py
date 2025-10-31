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

logger = logging.getLogger("telemetry-ingest")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

fleet_repo, _ = make_repos()
LAST_TELEM = {}


def handle_message(msg: Message):
    """Обработка входящих сообщений от MQTT."""
    try:
        topic = msg.topic
        payload = msg.payload

        parts = topic.split("/")
        if len(parts) < 3:
            logger.warning(f"Некорректный топик: {topic}")
            return

        _, veh_id, telem_type = parts
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                pass

        d = LAST_TELEM.setdefault(veh_id, {})
        d[telem_type] = payload

        logger.info(f"<< {telem_type} {veh_id}: {payload}")

    except Exception as e:
        logger.exception(f"Ошибка обработки телеметрии: {e}")


def main():
    logger.info("Telemetry Ingest запускается...")

    settings = Settings()
    bus = MqttBus(settings.MQTT_URL, client_id="telemetry-ingest")

    # Подписываемся на все телеметрические топики
    bus.subscribe(TelemetryTopics.ALL, handle_message)

    # Запускаем MQTT-потоки
    print(f"[DEBUG] MQTT URL = {settings.MQTT_URL}")
    print("[DEBUG] Starting MqttBus...")
    bus.start()
    print("[DEBUG] MqttBus started, waiting 3s...")

    # Держим процесс живым
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Останавливаем Telemetry Ingest...")
        bus.stop()


if __name__ == "__main__":
    main()
