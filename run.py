#!/usr/bin/env python3
"""Точка входа с поддержкой нескольких режимов работы (backend-абстракция).

Читает SYSTEM_MODE из конфига и запускает соответствующий бэкенд.
Старый run_system.py продолжает работать как раньше.
"""
import asyncio
import os
import subprocess
import sys
import time
import socket
import yaml
from pathlib import Path

# src/ в PYTHONPATH — так же, как делает run_system.py и .env.dev
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from drone_core.config.settings import Settings
from drone_core.infra.backends.factory import create_backend


def ensure_mqtt(host: str = "127.0.0.1", port: int = 1883) -> None:
    """Проверяет MQTT брокер, запускает если не найден."""
    try:
        with socket.create_connection((host, port), timeout=1):
            print(f"✅ MQTT брокер уже запущен на {host}:{port}")
            return
    except OSError:
        pass

    print("⚙️  MQTT брокер не найден, пробуем запустить...")
    try:
        subprocess.Popen(
            ["mosquitto", "-v"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
        with socket.create_connection((host, port), timeout=1):
            print(f"✅ MQTT брокер запущен ({host}:{port})")
    except (FileNotFoundError, OSError):
        print("❌ Не удалось запустить MQTT. Установи: brew install mosquitto")


def run_component(name: str, cmd: list[str], cwd: str | None = None):
    """Запускает компонент как подпроцесс."""
    print(f"▶️  {name}: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        stderr=subprocess.STDOUT,
        text=True,
    )


async def main():
    base_dir = Path(__file__).resolve().parent
    settings = Settings()
    mode = settings.SYSTEM_MODE

    print(f"🚀 Запуск системы в режиме: {mode}")

    # Загружаем конфиг симулятора
    cfg_path = base_dir / "src" / "simulator" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())

    ensure_mqtt()

    # Создаём и запускаем бэкенд
    backend = create_backend(mode)
    await backend.start(cfg)

    connected = await backend.get_connected_drones()
    print(f"✅ Бэкенд '{mode}' запущен. Подключено дронов: {len(connected)}")

    # Запуск сервисов (telemetry_ingest, orchestrator, web_ui)
    telemetry = run_component(
        "Telemetry Ingest",
        ["python", "-m", "drone_core.workers.telemetry_ingest"],
        cwd="src",
    )
    time.sleep(0.4)

    orchestrator = run_component(
        "Orchestrator",
        ["python", "-m", "drone_core.workers.orchestrator"],
        cwd="src",
    )
    time.sleep(0.4)

    web_ui = run_component(
        "Web UI",
        ["uvicorn", "web_ui.main:app", "--port", "8000", "--log-level", "info"],
        cwd="src",
    )

    print("\n🚀 Все компоненты запущены!")
    print("Открой UI → http://127.0.0.1:8000/static/index.html")

    services = [telemetry, orchestrator, web_ui]

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\n🧹 Завершаем...")
    finally:
        # Останавливаем сервисы
        for p in services:
            if p and p.poll() is None:
                p.terminate()
        time.sleep(1)
        for p in services:
            if p and p.poll() is None:
                p.kill()

        # Останавливаем бэкенд
        await backend.stop()
        print("✅ Система остановлена.")


if __name__ == "__main__":
    asyncio.run(main())