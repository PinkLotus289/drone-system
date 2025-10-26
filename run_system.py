#!/usr/bin/env python3
import subprocess
import time
import os
import yaml
import asyncio
from pathlib import Path
from mavsdk import System
from src.simulator.px4_launcher import start_px4_instances


# === MQTT ===
def ensure_mqtt():
    print("🔌 Проверяем MQTT брокер...")
    os.system("docker rm -f mosquitto-local > /dev/null 2>&1")
    os.system("docker run -d --name mosquitto-local -p 1883:1883 eclipse-mosquitto:2")
    print("✅ MQTT брокер запущен (eclipse-mosquitto:2)")


# === Подключение MAVSDK к PX4 ===
async def connect_to_px4(drone_id: int, port: int, timeout: int = 120):
    drone = System()
    addr = f"udp://:{port}"
    print(f"[MAVSDK-{drone_id}] ⏳ Подключаемся к PX4 через {addr} (ожидание до {timeout} с)...")
    await drone.connect(system_address=addr)

    start = time.time()
    while True:
        async for state in drone.core.connection_state():
            if state.is_connected:
                print(f"[MAVSDK-{drone_id}] ✅ Подключено к PX4!")
                return drone
        if time.time() - start > timeout:
            raise TimeoutError(f"[MAVSDK-{drone_id}] ❌ Не удалось подключиться к PX4 за {timeout} секунд")
        await asyncio.sleep(1)


# === Запуск подпроцессов ===
def run_component(name: str, cmd: list[str]):
    print(f"▶️  {name}: {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


# === Основной запуск ===
async def start_all():
    base_dir = Path(__file__).resolve().parent
    cfg_path = base_dir / "src/simulator/config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())

    ensure_mqtt()

    # 1️⃣ Запуск PX4 и ожидание готовности MAVLink
    procs = await start_px4_instances(cfg)

    # 2️⃣ Ожидание подключения MAVSDK ко всем PX4
    print("\n🕹️  Подключаем MAVSDK ко всем PX4-дронам...")
    tasks = []
    for d in cfg["drones"]:
        port = d["mavlink_out"]
        tasks.append(connect_to_px4(d["id"], port, timeout=120))

    # ждём, пока все дроны подключатся
    drones = await asyncio.gather(*tasks)
    print("✅ Все MAVSDK-соединения установлены!")

    # 3️⃣ Теперь можно запускать остальные сервисы
    print("▶️  Telemetry Ingest: python -m drone_core.workers.telemetry_ingest")
    telemetry = run_component("Telemetry Ingest", ["python", "-m", "drone_core.workers.telemetry_ingest"])

    print("▶️  Orchestrator: python -m drone_core.workers.orchestrator")
    orchestrator = run_component("Orchestrator", ["python", "-m", "drone_core.workers.orchestrator"])

    print("▶️  Web UI: uvicorn web_ui.main:app --port 8000")
    web_ui = run_component("Web UI", ["uvicorn", "web_ui.main:app", "--port", "8000"])

    print("\n🚀 Все компоненты запущены!")
    print("Открой UI → http://127.0.0.1:8000/static/index.html")

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\n🧹 Завершаем все процессы...")
    finally:
        all_procs = [telemetry, orchestrator, web_ui, *procs]
        for p in all_procs:
            if p and p.poll() is None:
                p.terminate()
        time.sleep(2)
        for p in all_procs:
            if p and p.poll() is None:
                p.kill()
        os.system("pkill -f px4 > /dev/null 2>&1")
        print("✅ Система остановлена и все PX4-процессы убиты.")


if __name__ == "__main__":
    asyncio.run(start_all())
