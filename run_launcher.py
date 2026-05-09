#!/usr/bin/env python3
"""Top-level entrypoint для нового web-launcher'а.

Запускает MQTT-брокер и FastAPI на :8000. Открой http://127.0.0.1:8000/ —
страница выбора режима:
    • SIM         — симуляция (PX4 SITL + виртуальные дроны)
    • REAL CONNECT — подключение/настройка реального борта
    • FLEET       — управление флотом реальных дронов (в разработке)

Тяжёлые подсистемы (PX4, MAVSDK bridges, workers) спавнятся через UI
по нажатию кнопки — этот скрипт ничего лишнего не запускает.

Альтернативные энтри-пойнты:
    run_system.py — прямой запуск sim-стэка из CLI (старый способ)
    run.py        — backend-factory вариант
"""
import atexit
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

CHILDREN: list[subprocess.Popen] = []
_CLEANED = False


def ensure_mqtt() -> None:
    host, port = "127.0.0.1", 1883

    def is_open() -> bool:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            return False

    print("🔌 Проверяем MQTT брокер...")
    if is_open():
        print(f"✅ MQTT брокер уже запущен на {host}:{port}")
        return

    print("⚙️  MQTT брокер не найден, пробуем запустить локально...")
    try:
        p = subprocess.Popen(
            ["mosquitto", "-v"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        CHILDREN.append(p)
        time.sleep(1)
        if is_open():
            print(f"✅ MQTT брокер успешно запущен ({host}:{port})")
        else:
            print("❌ Не удалось запустить локальный MQTT брокер.")
    except FileNotFoundError:
        print("❌ Mosquitto не найден. brew install mosquitto")


def cleanup_children() -> None:
    """Прибивает sim-подсистему (если её запускали через UI) + uvicorn + mosquitto."""
    global _CLEANED
    if _CLEANED:
        return
    _CLEANED = True

    sys.stdout.write("\n🧹 Завершаем все процессы...\n")
    sys.stdout.flush()

    # Те же паттерны, что в sim_control.py.
    patterns = (
        "bin/px4",
        "mavsdk_server",
        "simulator.mavsdk_bridge",
        "drone_core.workers.telemetry_ingest",
        "drone_core.workers.orchestrator",
        "uvicorn web_ui",
    )
    for patt in patterns:
        try:
            subprocess.run(
                ["pkill", "-9", "-f", patt],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=3,
            )
        except Exception:
            pass

    sys.stdout.write("✅ Система остановлена.\n")
    sys.stdout.flush()


atexit.register(cleanup_children)
for _sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    try:
        signal.signal(_sig, lambda *_: (cleanup_children(), sys.exit(0)))
    except Exception:
        pass  # Windows


def main() -> None:
    ensure_mqtt()

    src_dir = Path(__file__).resolve().parent / "src"
    print("\n🚀 Стартую Web Launcher...")
    print("Открой → http://127.0.0.1:8000/")
    print("Остановить: Ctrl+C\n")

    try:
        subprocess.run(
            ["uvicorn", "web_ui.main:app",
             "--host", "127.0.0.1", "--port", "8000",
             "--log-level", "info"],
            cwd=str(src_dir),
            check=False,
        )
    finally:
        cleanup_children()


if __name__ == "__main__":
    main()
