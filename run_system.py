#!/usr/bin/env python3
import subprocess
import time
import os
import yaml
import asyncio
from pathlib import Path
from mavsdk import System
from src.simulator.px4_launcher import start_px4_instances
import socket

# === MQTT ===
def ensure_mqtt():
    """Проверяет, работает ли локальный брокер MQTT, и запускает его при необходимости."""
    host = "127.0.0.1"
    port = 1883

    def is_port_open(host, port):
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            return False

    print("🔌 Проверяем MQTT брокер...")

    # Проверяем, запущен ли уже брокер
    if is_port_open(host, port):
        print(f"✅ MQTT брокер уже запущен на {host}:{port}")
        return

    print("⚙️  MQTT брокер не найден, пробуем запустить локально...")
    try:
        # Запускаем Mosquitto как фоновый процесс
        subprocess.Popen(
            ["mosquitto", "-v"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)

        if is_port_open(host, port):
            print(f"✅ MQTT брокер успешно запущен локально ({host}:{port})")
        else:
            print("❌ Не удалось запустить локальный MQTT брокер. Проверь установку Mosquitto.")
            print("   Подсказка: brew install mosquitto")
    except FileNotFoundError:
        print("❌ Mosquitto не найден в системе.")
        print("   Установи через Homebrew: brew install mosquitto")


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
def run_component(name: str, cmd: list[str], cwd: str | None = None):
    """Запускает компонент как подпроцесс с видимым логом"""
    print(f"▶️  {name}: {' '.join(cmd)} (cwd={cwd or os.getcwd()})")
    #log_path = Path(f"{name.lower().replace(' ', '_')}.log")
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        #stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
        text=True
    )

'''
def run_component(name: str, cmd: list[str], cwd: str | None = None):
    print(f"▶️  {name}: {' '.join(cmd)} (cwd={cwd or os.getcwd()})")
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    # Читаем первые строки, чтобы понять, что реально происходит
    try:
        for _ in range(10):
            line = proc.stdout.readline()
            if not line:
                break
            print(f"[{name}] {line.strip()}")
    except Exception as e:
        print(f"[{name}] Ошибка чтения stdout: {e}")
    return proc
'''

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

    # ▶️  MAVSDK Bridge — публикует телеметрию PX4 → MQTT
    print("▶️  MAVSDK Bridge: python -m simulator.mavsdk_bridge")
    mavsdk_bridge = run_component(
        "MAVSDK Bridge",
        ["python", "-m", "simulator.mavsdk_bridge"],
        cwd="src"
    )

    # 3️⃣ Теперь можно запускать остальные сервисы
    print("▶️  Telemetry Ingest: python -m drone_core.workers.telemetry_ingest")
    telemetry = run_component("Telemetry Ingest", ["python", "-m", "drone_core.workers.telemetry_ingest"], cwd="src")

    print("▶️  Orchestrator: python -m drone_core.workers.orchestrator")
    orchestrator = run_component("Orchestrator", ["python", "-m", "drone_core.workers.orchestrator"], cwd="src")

    print("▶️  Web UI: uvicorn web_ui.main:app --port 8000")
    web_ui = run_component(
        "Web UI",
        ["uvicorn", "web_ui.main:app", "--port", "8000", "--reload", "--log-level", "debug"],
        cwd="src"
    )

    print("\n🚀 Все компоненты запущены!")
    print("Открой UI → http://127.0.0.1:8000/static/index.html")

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\n🧹 Завершаем все процессы...")
    finally:
        all_procs = [telemetry, orchestrator, web_ui, mavsdk_bridge, *procs]
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
