#!/usr/bin/env python3
import atexit
import os
import signal
import socket
import subprocess
import sys
import time
import yaml
import asyncio
from pathlib import Path

from src.simulator.px4_launcher import start_px4_instances


# =============================================================
#  Глобальный реестр подпроцессов + надёжный cleanup.
#  Каждый ребёнок стартует в своей process group (start_new_session=True),
#  чтобы на выходе killpg прибил его вместе с ВСЕМИ внуками (mavsdk_server,
#  Cesium worker threads и пр.), а не только «главный» pid.
#  На SIGINT/SIGTERM/SIGHUP/atexit — выполняем cleanup.
# =============================================================
CHILDREN: list[subprocess.Popen] = []
_CLEANED = False


def ensure_mqtt():
    host, port = "127.0.0.1", 1883

    def is_open():
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


def run_component(name: str, cmd: list[str], cwd: str | None = None, env: dict | None = None):
    print(f"▶️  {name}: {' '.join(cmd)} (cwd={cwd or os.getcwd()})")
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    p = subprocess.Popen(
        cmd,
        cwd=cwd,
        stderr=subprocess.STDOUT,
        text=True,
        env=run_env,
        # Отдельная сессия/группа — чтобы killpg на выходе прибил всех внуков тоже.
        start_new_session=True,
    )
    CHILDREN.append(p)
    return p


def cleanup_children():
    """Убивает всех детей по cmdline-паттерну. Надёжно независимо от process-group."""
    global _CLEANED
    if _CLEANED:
        return
    _CLEANED = True

    sys.stdout.write("\n🧹 Завершаем все процессы...\n")
    sys.stdout.flush()

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


# =============================================================
#  Main pipeline
# =============================================================
async def start_all():
    base_dir = Path(__file__).resolve().parent
    cfg_path = base_dir / "src/simulator/config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())

    # asyncio-нативная обработка сигналов: signal.signal() не работает
    # в asyncio.run() loop — event loop перехватывает signals. loop.add_signal_handler
    # ставит asyncio-compatible handler, который точно сработает.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for _sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            loop.add_signal_handler(_sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows

    ensure_mqtt()

    # 1) PX4 SITL instances
    px4_procs = await start_px4_instances(cfg)
    CHILDREN.extend(px4_procs)

    # 2) Небольшая пауза для PX4
    print("\n🕹️  PX4-инстансы готовы, bridge подключит MAVSDK самостоятельно.")
    await asyncio.sleep(1.0)

    # 3) MAVSDK Bridge — по одному процессу на дрон
    for d in cfg["drones"]:
        did = str(d["id"])
        print(f"▶️  MAVSDK Bridge for drone {did}")
        run_component(
            f"MAVSDK Bridge {did}",
            ["python", "-m", "simulator.mavsdk_bridge"],
            cwd="src",
            env={"DRONE_ID": did},
        )
        time.sleep(0.4)

    # 4) Прочие сервисы
    run_component("Telemetry Ingest", ["python", "-m", "drone_core.workers.telemetry_ingest"], cwd="src")
    time.sleep(0.4)

    run_component("Orchestrator", ["python", "-m", "drone_core.workers.orchestrator"], cwd="src")
    time.sleep(0.4)

    run_component(
        "Web UI",
        ["uvicorn", "web_ui.main:app", "--port", "8000", "--log-level", "info"],
        cwd="src",
    )

    print("\n🚀 Все компоненты запущены!")
    print("Открой UI → http://127.0.0.1:8000/   (откроется вход; код по умолчанию: skybite)")
    print("Остановить:  Ctrl+C  (все подпроцессы будут убиты автоматически)\n")

    # Ждём сигнала остановки или падения main-loop.
    await stop_event.wait()
    print("\n📡 Получен сигнал остановки.")


if __name__ == "__main__":
    try:
        asyncio.run(start_all())
    finally:
        # Cleanup ВСЕГДА: нормальный выход, Ctrl+C, SIGTERM, SIGHUP (терминал закрыт).
        cleanup_children()
