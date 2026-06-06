"""Роуты раздела «Подключить реальный дрон».

CRUD профилей дронов + auto-discovery serial-портов + пробное MAVLink-подключение
для проверки готовности борта (heartbeat, GPS fix, батарея, калибровки).
"""
from __future__ import annotations
import asyncio
import glob
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from drone_core.config.settings import Settings
from drone_core.profiles import DroneProfile, profile_store
from . import auth as authmod
from .real_control import real_supervisor
from .radio_relay import radio_manager

router = APIRouter()
settings = Settings()

APP_ROOT = Path(__file__).parents[1]
STATIC_DIR = APP_ROOT / "web_ui" / "static"


# =====================================================
#  Страница
# =====================================================
@router.get("/real")
async def real_page():
    return FileResponse(str(STATIC_DIR / "real_connect.html"))


# =====================================================
#  Профили: CRUD
# =====================================================
@router.get("/api/real/profiles")
async def list_profiles() -> Dict[str, List[Dict[str, Any]]]:
    return {"profiles": [p.model_dump() for p in profile_store.list()]}


@router.get("/api/real/profiles/{name}")
async def get_profile(name: str):
    p = profile_store.get(name)
    if p is None:
        raise HTTPException(status_code=404, detail="profile_not_found")
    return p.model_dump()


@router.post("/api/real/profiles")
async def save_profile(body: Dict[str, Any]):
    try:
        profile = DroneProfile.model_validate(body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid_profile: {e}")
    saved = profile_store.save(profile)
    return saved.model_dump()


@router.delete("/api/real/profiles/{name}")
async def delete_profile(name: str):
    ok = profile_store.delete(name)
    if not ok:
        raise HTTPException(status_code=404, detail="profile_not_found")
    return {"ok": True}


# =====================================================
#  Auto-discovery serial-портов
# =====================================================
@router.get("/api/real/serial-ports")
async def list_serial_ports() -> Dict[str, List[Dict[str, str]]]:
    if sys.platform == "darwin":
        patterns = [
            "/dev/cu.usbserial*",  # FTDI/CP210x — типичный SiK radio
            "/dev/cu.usbmodem*",   # CDC ACM — Pixhawk USB-C
            "/dev/cu.SLAB*",
            "/dev/cu.wchusbserial*",
        ]
    elif sys.platform.startswith("linux"):
        patterns = ["/dev/ttyUSB*", "/dev/ttyACM*"]
    else:
        patterns = []

    seen: set[str] = set()
    out: List[Dict[str, str]] = []
    for pat in patterns:
        for p in glob.glob(pat):
            if p in seen:
                continue
            seen.add(p)
            label = os.path.basename(p)
            hint = ""
            if "usbserial" in p or "SLAB" in p or "wchusb" in p:
                hint = "SiK / FTDI"
            elif "usbmodem" in p or "ACM" in p:
                hint = "USB-C / CDC"
            out.append({"path": p, "label": label, "hint": hint})
    return {"ports": out}


# =====================================================
#  Test connection — пробное MAVLink-подключение
# =====================================================
class TestConnRequest(BaseModel):
    connection_type: str
    serial_port: Optional[str] = None
    serial_baud: Optional[int] = 57600
    net_host: Optional[str] = None
    net_port: Optional[int] = None
    timeout_s: float = 12.0


def _build_url(req: TestConnRequest) -> str:
    if req.connection_type == "serial":
        if not req.serial_port:
            raise ValueError("serial_port обязателен")
        return f"serial://{req.serial_port}:{req.serial_baud or 57600}"
    if req.connection_type == "udp":
        host = req.net_host or ""
        if req.net_port is None:
            raise ValueError("net_port обязателен")
        return f"udp://{host}:{req.net_port}"
    if req.connection_type == "tcp":
        if not req.net_host or req.net_port is None:
            raise ValueError("net_host и net_port обязательны")
        return f"tcp://{req.net_host}:{req.net_port}"
    raise ValueError(f"unknown connection_type: {req.connection_type}")


# Уникальные gRPC-порты для тестов, чтобы не конфликтовали с sim mavsdk-серверами
# (sim использует 50151+id). Циклическая выдача в высоком диапазоне.
_TEST_GRPC_PORT = 50191
_TEST_GRPC_LOCK = asyncio.Lock()


async def _run_test_connection(connection_url: str, timeout_s: float) -> Dict[str, Any]:
    """Подключается к борту через MAVSDK, читает heartbeat и здоровье. Возвращает dict.

    Не делает arm/takeoff и НЕ изменяет параметры PX4 — только чтение.
    """
    # Импорт внутри функции: mavsdk дорогой, грузим только когда тестируем.
    from mavsdk import System

    global _TEST_GRPC_PORT
    async with _TEST_GRPC_LOCK:
        grpc_port = _TEST_GRPC_PORT
        _TEST_GRPC_PORT = 50191 if _TEST_GRPC_PORT >= 50220 else _TEST_GRPC_PORT + 1

    drone = System(port=grpc_port)
    result: Dict[str, Any] = {
        "ok": False,
        "connection_url": connection_url,
        "grpc_port": grpc_port,
        "started_at": time.time(),
    }

    async def _do() -> Dict[str, Any]:
        await drone.connect(system_address=connection_url)

        # 1) Heartbeat
        async for state in drone.core.connection_state():
            if state.is_connected:
                result["heartbeat"] = True
                break

        # 2) Firmware/identification (best-effort).
        try:
            info = await drone.info.get_version()
            result["firmware"] = {
                "flight_sw_major": info.flight_sw_major,
                "flight_sw_minor": info.flight_sw_minor,
                "flight_sw_patch": info.flight_sw_patch,
                "os_sw_major": getattr(info, "os_sw_major", None),
            }
        except Exception as e:
            result["firmware_error"] = repr(e)

        # 3) Health-флаги (sample 1 шт).
        try:
            async for h in drone.telemetry.health():
                result["health"] = {
                    "gyro_ok": h.is_gyrometer_calibration_ok,
                    "accel_ok": h.is_accelerometer_calibration_ok,
                    "mag_ok": h.is_magnetometer_calibration_ok,
                    "local_pos_ok": h.is_local_position_ok,
                    "global_pos_ok": h.is_global_position_ok,
                    "home_ok": h.is_home_position_ok,
                    "armable": h.is_armable,
                }
                break
        except Exception as e:
            result["health_error"] = repr(e)

        # 4) GPS-инфо.
        try:
            async for gps in drone.telemetry.gps_info():
                fix = getattr(gps.fix_type, "name", str(gps.fix_type))
                result["gps"] = {
                    "satellites": gps.num_satellites,
                    "fix_type": fix,
                }
                break
        except Exception as e:
            result["gps_error"] = repr(e)

        # 5) Батарея.
        try:
            async for bat in drone.telemetry.battery():
                result["battery"] = {
                    "voltage_v": bat.voltage_v,
                    "remaining_percent": bat.remaining_percent,
                }
                break
        except Exception as e:
            result["battery_error"] = repr(e)

        # 6) Flight mode (что у борта сейчас).
        try:
            async for m in drone.telemetry.flight_mode():
                result["flight_mode"] = str(m)
                break
        except Exception as e:
            result["flight_mode_error"] = repr(e)

        result["ok"] = bool(result.get("heartbeat"))
        return result

    try:
        return await asyncio.wait_for(_do(), timeout=timeout_s)
    except asyncio.TimeoutError:
        result["error"] = "timeout"
        result["message"] = f"Нет heartbeat за {timeout_s:.1f}с — проверь кабель/радио/baud"
        return result
    except Exception as e:
        result["error"] = type(e).__name__
        result["message"] = str(e)
        return result
    finally:
        # mavsdk_server остановится при сборке мусора объекта `drone`.
        # Явный close MAVSDK-Python не предоставляет.
        del drone


@router.post("/api/real/test-connection")
async def test_connection(req: TestConnRequest):
    try:
        url = _build_url(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await _run_test_connection(url, req.timeout_s)


# =====================================================
#  Connect / disconnect реального борта (мост к нему)
# =====================================================
@router.post("/api/real/connect/{name}")
async def connect_real(name: str):
    profile = profile_store.get(name)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile_not_found")
    # Radio: связь идёт через браузерный Web Serial → WS → локальное реле.
    # MAVSDK-мост цепляется к реле по localhost tcp, а не к железу напрямую.
    if profile.connection_type == "radio":
        relay = await radio_manager.ensure(name)
        url = f"tcp://127.0.0.1:{relay.tcp_port}"
        return real_supervisor.connect(profile, url_override=url)
    return real_supervisor.connect(profile)


@router.post("/api/real/radio/test/{name}")
async def radio_test(name: str):
    """Read-only проверка радио-линка ЧЕРЕЗ реле: MAVSDK цепляется к relay tcp,
    байты идут браузер↔USB-радио↔борт. Браузерный агент уже должен быть подключён
    (страница поднимает Web Serial перед вызовом). Если агента нет — будет timeout."""
    relay = await radio_manager.ensure(name)
    url = f"tcp://127.0.0.1:{relay.tcp_port}"
    res = await _run_test_connection(url, 12.0)
    res["agent_connected"] = relay.agent_connected
    if not res.get("agent_connected") and not res.get("heartbeat"):
        res.setdefault("message", "Браузерный радио-агент не подключён — нет потока от борта")
    return res


@router.post("/api/real/disconnect/{name}")
async def disconnect_real(name: str):
    res = real_supervisor.disconnect(name)
    # Для radio дополнительно гасим реле (закроет и браузерный агент).
    if radio_manager.get(name) is not None:
        await radio_manager.teardown(name)
    return res


@router.get("/api/real/connections")
async def real_connections():
    return {"connections": real_supervisor.status(), "radio": radio_manager.status()}


# =====================================================
#  Radio: WebSocket «связного» из браузера (Web Serial)
# =====================================================
@router.websocket("/api/real/radio/agent")
async def radio_agent_ws(websocket: WebSocket, name: str):
    """Браузерный агент: гонит сырые MAVLink-байты USB-радио ↔ сервер.

    Авторизация — по той же session-cookie, что и весь UI (агент работает во
    вкладке залогиненного оператора). Канал бинарный в обе стороны.
    """
    payload = authmod.operator_from_cookies(websocket.cookies, settings.SESSION_SECRET)
    if payload is None:
        await websocket.close(code=1008)  # policy violation — не залогинен
        return
    # name должен быть валидным slug профиля (защита от мусора в ключе реле).
    if not name or not all(c.isalnum() or c in "_-" for c in name) or len(name) > 40:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    relay = await radio_manager.attach_agent(name, websocket)
    print(f"[radio] agent attached for '{name}' (op={payload.get('op')})")
    try:
        while True:
            data = await websocket.receive_bytes()
            await relay.feed_from_agent(data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[radio] agent '{name}' ws error: {e}")
    finally:
        relay.clear_agent(websocket)
        print(f"[radio] agent detached for '{name}'")