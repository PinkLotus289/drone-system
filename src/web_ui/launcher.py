"""Лаунчер-роуты: страница выбора режима + управление sim-supervisor'ом."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

from .sim_control import sim_supervisor

router = APIRouter()
APP_ROOT = Path(__file__).parents[1]
STATIC_DIR = APP_ROOT / "web_ui" / "static"


@router.get("/")
async def launcher_page():
    return FileResponse(str(STATIC_DIR / "launcher.html"))


@router.get("/api/launcher/sim/status")
async def sim_status():
    return sim_supervisor.status()


@router.post("/api/launcher/sim/start")
async def sim_start(body: Optional[Dict[str, Any]] = Body(None)):
    num_drones = None
    if body and "num_drones" in body:
        try:
            num_drones = int(body["num_drones"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_num_drones"}
    return await sim_supervisor.start(num_drones=num_drones)


@router.post("/api/launcher/sim/stop")
async def sim_stop():
    return await sim_supervisor.stop()