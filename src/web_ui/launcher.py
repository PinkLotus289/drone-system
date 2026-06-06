"""Лаунчер-роуты: страница выбора режима + управление sim-supervisor'ом."""
from __future__ import annotations
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

from .sim_control import sim_supervisor

router = APIRouter()
APP_ROOT = Path(__file__).parents[1]
STATIC_DIR = APP_ROOT / "web_ui" / "static"

# Момент старта процесса web-сервера — основа для аптайма в шапке лобби.
_STARTED_AT = time.monotonic()


@router.get("/")
async def launcher_page():
    return FileResponse(str(STATIC_DIR / "launcher.html"))


@router.get("/api/launcher/health")
async def launcher_health():
    """Лёгкий liveness-пробник: факт ответа = система жива, плюс аптайм сервера."""
    return {"ok": True, "uptime_seconds": int(time.monotonic() - _STARTED_AT)}


@router.get("/api/launcher/sim/status")
async def sim_status():
    return sim_supervisor.status()


def _reset_session_state() -> None:
    """Сброс эфемерного состояния UI (дроны/миссии/трейлы) при (пере)запуске sim'а.
    Ленивый импорт — main.py импортирует этот модуль, прямой импорт был бы циклом."""
    try:
        from .main import reset_sim_session_state
        reset_sim_session_state()
    except Exception as e:  # не блокируем старт/стоп из-за сброса
        print(f"[launcher] reset_sim_session_state failed: {e}")


@router.post("/api/launcher/sim/start")
async def sim_start(body: Optional[Dict[str, Any]] = Body(None)):
    num_drones = None
    if body and "num_drones" in body:
        try:
            num_drones = int(body["num_drones"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_num_drones"}
    # Чистим состояние прошлого запуска ДО старта — новый кластер стартует с нуля.
    _reset_session_state()
    return await sim_supervisor.start(num_drones=num_drones)


@router.post("/api/launcher/sim/stop")
async def sim_stop():
    result = await sim_supervisor.stop()
    # После убийства бортов/воркеров чистим и UI-состояние, чтобы в лобби/при
    # следующем заходе не висели призраки прошлой симуляции.
    _reset_session_state()
    return result