from __future__ import annotations
import json
import asyncio
import yaml
from pathlib import Path
from typing import Any, Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.datastructures import State
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from drone_core.config.settings import Settings
from drone_core.infra.repositories.fleet_mem import FleetMem
from drone_core.infra.repositories.missions_mem import MissionsMem
from drone_core.domain.models import Order, LLA
from drone_core.infra.messaging.mqtt_bus import MqttBus

from .launcher import router as launcher_router
from .real import router as real_router

# --- пути и настройки ---
APP_ROOT = Path(__file__).parents[1]
PROJECT_ROOT = APP_ROOT.parent
SIM_CFG = APP_ROOT / "simulator" / "config.yaml"
MISSION_CTX = PROJECT_ROOT / "config" / "mission_context.yaml"

app = FastAPI(title="Drone System Dashboard")
app.state: State
app.mount(
    "/static",
    StaticFiles(directory=str(APP_ROOT / "web_ui" / "static")),
    name="static",
)

# Лаунчер (страница выбора режима, /) и раздел real-drone (форма, профили, /real).
# Подключаем ПЕРЕД остальными роутами, чтобы / отдавал лаунчер.
app.include_router(launcher_router)
app.include_router(real_router)

settings = Settings()
bus = MqttBus(settings.MQTT_URL, client_id="ui-bus")
fleet_repo = FleetMem()
missions_repo = MissionsMem()
telemetry_clients: set[WebSocket] = set()
active_drones: dict[str, dict] = {}


def read_cfg() -> Dict[str, Any]:
    with open(SIM_CFG, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# === Startup ===
@app.on_event("startup")
async def _startup():
    print("[UI] Starting MqttBus...")
    bus.start()

    # Главный event loop FastAPI
    main_loop = asyncio.get_running_loop()

    # Сохраняем активных дронов + активные миссии в памяти
    app.state.active_drones = {}
    app.state.active_missions = {}  # {mission_id: {...}}
    # Буфер траекторий: {drone_id: [[lon,lat,alt], ...]} — чтобы трейлы переживали
    # перезагрузку страницы (фронт восстанавливает их из снапшота при загрузке).
    app.state.drone_trails = {}
    _TRAIL_MAX = 600

    def _update_mission(mid: str, **fields):
        import time as _t
        m = app.state.active_missions.setdefault(mid, {
            "mission_id": mid,
            "vehicle_id": None,
            "status": "PLANNED",
            "progress_current": 0,
            "progress_total": 0,
            "waypoints": [],
        })
        m.update({k: v for k, v in fields.items() if v is not None})
        m["updated"] = _t.time()
        # Терминальную миссию (выполнена/отменена/ошибка) держим в списке ещё 20с
        # (UI показывает её как COMPLETED/100% / failed), потом убираем — линия
        # таймлайна исчезает. ABORTED/FAILED тоже чистим, иначе они зависают.
        _status = str(m.get("status") or "")
        _terminal = _status == "COMPLETED" or _status == "ABORTED" or "FAILED" in _status
        if _terminal:
            async def _cleanup(_mid=mid):
                await asyncio.sleep(20)
                app.state.active_missions.pop(_mid, None)
            main_loop.call_soon_threadsafe(asyncio.create_task, _cleanup())

    # --- обработчик сообщений MQTT ---
    def _mqtt_handler(message):
        try:
            raw = message.payload
            # Универсальный парсер payload
            if isinstance(raw, (bytes, bytearray)):
                text = raw.decode("utf-8", errors="ignore").strip()
                data = json.loads(text) if text.startswith("{") else {"raw": text}
            elif isinstance(raw, (dict, list)):
                data = raw
            elif isinstance(raw, str):
                text = raw.strip()
                data = json.loads(text) if text.startswith("{") else {"raw": text}
            else:
                data = {}
        except Exception as e:
            print(f"[UI] ⚠️ Ошибка парсинга MQTT payload: {e}")
            data = {}

        topic = message.topic
        msg = {"topic": topic, "payload": data}

        # === Обработка типов сообщений ===
        if topic == "fleet/active":
            msg["type"] = "drone_active"
            d = data if isinstance(data, dict) else {}
            drone_id = d.get("id", f"drone_{len(app.state.active_drones)}")
            app.state.active_drones[drone_id] = {
                "id": drone_id,
                "name": d.get("name", drone_id),
                "lat": d.get("lat"),
                "lon": d.get("lon"),
                "alt": d.get("alt", 0),
                "status": d.get("status", "IDLE"),
            }

        elif topic.startswith("telem/"):
            msg["type"] = "telemetry_update"

            # Извлекаем ID дрона + тип потока: telem/veh_0/pose|battery|attitude|gps|velocity|actuators
            parts = topic.split("/")
            drone_id = parts[1] if len(parts) > 1 else "unknown"
            stream = parts[2] if len(parts) > 2 else "pose"

            # Регистрируем дрона если его ещё нет
            if drone_id not in app.state.active_drones:
                app.state.active_drones[drone_id] = {
                    "id": drone_id,
                    "name": drone_id,
                    "lat": None,
                    "lon": None,
                    "alt": 0,
                    "status": "ACTIVE",
                }

            d = app.state.active_drones[drone_id]
            if isinstance(data, dict):
                # Маршрутизация по типу потока — обновляем подсектор state дрона
                if stream == "pose":
                    d["lat"] = data.get("lat", d.get("lat"))
                    d["lon"] = data.get("lon", d.get("lon"))
                    d["alt"] = data.get("alt", d.get("alt"))
                    # копим трейл (формат [lon,lat,alt] как на фронте) с дедупом и капом
                    try:
                        plat = data.get("lat"); plon = data.get("lon")
                        if plat is not None and plon is not None:
                            buf = app.state.drone_trails.setdefault(drone_id, [])
                            palt = float(data.get("alt") or 0.0)
                            if (not buf) or abs(buf[-1][0] - plon) > 1e-7 or abs(buf[-1][1] - plat) > 1e-7 or abs((buf[-1][2] or 0) - palt) > 0.3:
                                buf.append([float(plon), float(plat), max(0.0, palt)])
                                if len(buf) > _TRAIL_MAX:
                                    del buf[0:len(buf) - _TRAIL_MAX]
                    except Exception:
                        pass
                elif stream == "battery":
                    d["battery_v"] = data.get("voltage_v")
                    d["battery_pct"] = data.get("remaining_pct")
                elif stream == "attitude":
                    d["pitch"] = data.get("pitch")
                    d["roll"] = data.get("roll")
                    d["yaw"] = data.get("yaw")
                elif stream == "gps":
                    d["sat_count"] = data.get("satellites")
                    d["fix_type"] = data.get("fix_type")
                elif stream == "velocity":
                    d["gs"] = data.get("gs")
                    d["vz"] = data.get("vz")
                elif stream == "actuators":
                    d["motors"] = data.get("motors")
                    d["motors_active"] = data.get("active")

        elif topic.endswith("/planned"):
            msg["type"] = "mission_planned"
            mid = topic.split("/")[1] if len(topic.split("/")) > 1 else None
            if mid and isinstance(data, dict):
                _update_mission(
                    mid,
                    status="PLANNED",
                    mission_type=data.get("mission_type", "delivery"),
                    takeoff_profile=data.get("takeoff_profile", "vertical"),
                    notes=data.get("notes"),
                    waypoints=data.get("waypoints", []),
                )
        elif topic.endswith("/assigned"):
            msg["type"] = "mission_assigned"
            mid = topic.split("/")[1] if len(topic.split("/")) > 1 else None
            if mid and isinstance(data, dict):
                _update_mission(
                    mid,
                    status="ASSIGNED",
                    vehicle_id=data.get("vehicle_id"),
                )
        elif topic.endswith("/status"):
            msg["type"] = "mission_status"
            mid = topic.split("/")[1] if len(topic.split("/")) > 1 else None
            if mid and isinstance(data, dict):
                status = data.get("status")
                _update_mission(mid, status=status, vehicle_id=data.get("vehicle_id"))
        elif topic.endswith("/progress"):
            msg["type"] = "mission_progress"
            mid = topic.split("/")[1] if len(topic.split("/")) > 1 else None
            if mid and isinstance(data, dict):
                _update_mission(
                    mid,
                    progress_current=data.get("current", 0),
                    progress_total=data.get("total", 0),
                    vehicle_id=data.get("vehicle_id"),
                    status="IN_PROGRESS" if int(data.get("current", 0)) > 0 else None,
                )

        # --- Отправка всем WebSocket клиентам ---
        async def _send_to_all():
            text = json.dumps(msg)
            for c in list(telemetry_clients):
                try:
                    await c.send_text(text)
                except Exception:
                    telemetry_clients.discard(c)

        main_loop.call_soon_threadsafe(asyncio.create_task, _send_to_all())

    # --- подписки на MQTT ---
    bus.subscribe("fleet/active", _mqtt_handler, qos=1)
    bus.subscribe("telem/+/+", _mqtt_handler, qos=0)
    bus.subscribe("mission/+/planned", _mqtt_handler, qos=1)
    bus.subscribe("mission/+/status", _mqtt_handler, qos=1)
    bus.subscribe("mission/+/assigned", _mqtt_handler, qos=1)
    bus.subscribe("mission/+/progress", _mqtt_handler, qos=0)


# === Маршруты API ===
# `/` обслуживает launcher_router (страница выбора режима).
# UI симулятора живёт на `/sim`.
@app.get("/sim")
async def sim_index():
    return FileResponse(str(APP_ROOT / "web_ui" / "static" / "index.html"))


@app.get("/testing")
async def testing_page():
    """Pre-flight testing wizard for real drones."""
    return FileResponse(str(APP_ROOT / "web_ui" / "static" / "testing.html"))


@app.get("/local_launch")
async def local_launch_page():
    """Direct-control launch page (single airframe, no mission plan)."""
    return FileResponse(str(APP_ROOT / "web_ui" / "static" / "local_launch.html"))


@app.get("/sim/drone/{drone_id}")
async def drone_detail_page(drone_id: str):
    """Detail view for a specific airframe in the fleet."""
    return FileResponse(str(APP_ROOT / "web_ui" / "static" / "drone_detail.html"))


@app.get("/api/base")
async def api_base():
    cfg = read_cfg()
    return cfg.get("base", {"lat": 43.07470, "lon": -89.38420})


# Дефолт на случай, если yaml потерян или невалиден — UI должен загружаться всегда.
_MISSION_CTX_DEFAULT: Dict[str, Any] = {
    "operation": {
        "name": "SKYBITE OPS",
        "number": "01",
        "sector": "K-7",
        "rel": "NATO",
        "classification": "UNCLASSIFIED // FOR OFFICIAL USE ONLY",
        "datalink_primary": "AES-256",
        "datalink_backup": "SAT-2 STBY",
    },
    "operator": {
        "name": "M. VOLK",
        "clearance": "CL-3",
        "session_id": "0x7F2A",
    },
    "certifications": ["FIPS 140-3", "DO-178C", "STANAG 4586", "STANAG 4609"],
    "keep_out_zones": [],
    "threats": [],
    "ui_defaults": {"aor_radius_m": 2000, "aor_inner_rings": [1000, 500]},
}


@app.get("/api/mission_context")
async def api_mission_context():
    """Витрина UI: имя операции, оператор, classification, KOZ/threats, серт-список.
    Читается из config/mission_context.yaml. Если файл отсутствует или битый — отдаётся дефолт,
    чтобы UI не падал и страница всегда грузилась."""
    if not MISSION_CTX.exists():
        return _MISSION_CTX_DEFAULT
    try:
        with open(MISSION_CTX, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # Сливаем с дефолтами на верхнем уровне ключей — на случай неполного yaml
        merged: Dict[str, Any] = {**_MISSION_CTX_DEFAULT, **data}
        # Глубокое слияние для словарных секций
        for k in ("operation", "operator", "ui_defaults"):
            if isinstance(merged.get(k), dict):
                merged[k] = {**_MISSION_CTX_DEFAULT[k], **(data.get(k) or {})}
        return merged
    except Exception as e:
        print(f"[UI] ⚠️ mission_context.yaml read failed: {e} — fallback to default")
        return _MISSION_CTX_DEFAULT


@app.get("/api/drones")
async def api_drones():
    """Возвращает список активных дронов, полученных по MQTT"""
    return {"drones": list(app.state.active_drones.values())}


@app.get("/api/missions")
async def api_missions():
    ms = await missions_repo.list_active()
    return [m.dict() for m in ms]


@app.get("/api/settings")
async def api_settings():
    cfg = read_cfg()
    return {
        "base": cfg.get("base", {"lat": 43.07470, "lon": -89.38420}),
        "drone_count": len((await fleet_repo.list_all()) or []),
    }


@app.post("/api/orders")
async def api_orders(body: Dict[str, Any]):
    """Создаёт заказ, публикует его в MQTT → orchestrator.

    Дополнительные поля (drone_id, priority, mission_type, cruise_alt_m,
    cruise_speed_mps, takeoff_alt_m, auto_rth, notes, и расширенные
    waypoints/loop_count/orbit_radius_m/pattern/sector_*) пробрасываются
    в payload MQTT под ключом `extras` — orchestrator может их учитывать
    либо игнорировать."""
    cfg = read_cfg()
    base_cfg = cfg.get("base", {"lat": 43.07470, "lon": -89.38420})
    base = LLA(lat=float(base_cfg["lat"]), lon=float(base_cfg["lon"]), alt=60.0)

    cruise_alt = float(body.get("cruise_alt_m") or 60.0)
    addr1 = body.get("from") or {"lat": body.get("pickup_lat"), "lon": body.get("pickup_lon"), "alt": cruise_alt}
    addr2 = body.get("to") or {"lat": body.get("drop_lat"), "lon": body.get("drop_lon"), "alt": cruise_alt}
    # Гарантируем alt в адресах (фронт может не прислать)
    addr1.setdefault("alt", cruise_alt)
    addr2.setdefault("alt", cruise_alt)
    payload_kg = float(body.get("weight", 2.0))

    if not addr1.get("lat") or not addr2.get("lat"):
        return {"error": "Missing coordinates"}

    priority = str(body.get("priority") or "normal").lower()
    if priority not in ("low", "normal", "high", "urgent"):
        priority = "normal"

    order = Order(
        base=base,
        addr1=LLA(**{k: addr1[k] for k in ("lat", "lon", "alt")}),
        addr2=LLA(**{k: addr2[k] for k in ("lat", "lon", "alt")}),
        payload_kg=payload_kg,
        priority=priority,
    )
    payload = order.dict()
    # extras — для orchestrator'а; backend пока их не использует.
    # waypoints/loop_count/orbit_radius_m/pattern/sector_* — расширенные поля
    # для мультимиссионных типов (ISR / Patrol / Sector). Если их нет — fallback
    # на классическую генерацию base→A→B→base в planner.
    extras: Dict[str, Any] = {
        "drone_id": body.get("drone_id"),
        "mission_type": body.get("mission_type") or "delivery",
        "cruise_alt_m": cruise_alt,
        "cruise_speed_mps": float(body.get("cruise_speed_mps") or 10.0),
        "takeoff_alt_m": float(body.get("takeoff_alt_m") or 15.0),
        "takeoff_profile": str(body.get("takeoff_profile") or "vertical").lower(),
        "auto_rth": bool(body.get("auto_rth", True)),
        "notes": (body.get("notes") or "").strip() or None,
    }
    for k in ("waypoints", "loop_count", "orbit_radius_m", "pattern",
              "sector_polygon", "sector_index", "sector_total"):
        if body.get(k) is not None:
            extras[k] = body.get(k)
    payload["extras"] = extras
    bus.publish("orders/new", payload)
    return {"status": "ok", "order_id": order.id}


@app.post("/api/start_mission")
async def start_mission():
    return {"status": "not_implemented"}


# === WebSocket ===
@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    telemetry_clients.add(websocket)
    print("🌐 WebSocket клиент подключен")

    try:
        # просто держим соединение живым
        while True:
            await asyncio.sleep(30)
    except WebSocketDisconnect:
        telemetry_clients.discard(websocket)
        print("❌ WebSocket отключен")

@app.get("/api/fleet")
async def api_fleet():
    """Возвращает весь флот с актуальной телеметрией"""
    return {"fleet": list(app.state.active_drones.values())}

@app.get("/api/system/mode")
async def api_system_mode():
    """Возвращает текущий режим системы (test / preflight / full)."""
    return {"mode": settings.SYSTEM_MODE}


@app.get("/api/free_drones")
async def api_free_drones():
    """Возвращает только свободных дронов"""
    free = [d for d in app.state.active_drones.values() if d.get("status") in ("IDLE", "ACTIVE")]
    return {"drones": free}


@app.get("/api/active_missions")
async def api_active_missions():
    """Возвращает список активных миссий с их текущим прогрессом."""
    ms = list(app.state.active_missions.values())
    # сортируем: невыполненные сверху, COMPLETED в конце
    ms.sort(key=lambda m: (m.get("status") == "COMPLETED", -m.get("updated", 0)))
    return {"missions": ms}


@app.get("/api/session/snapshot")
async def api_session_snapshot():
    """Снапшот сессии для восстановления карты после перезагрузки страницы:
    активные миссии (с waypoints/типом/профилем взлёта) + траектории дронов.
    Дроны восстанавливаются отдельно через /api/fleet."""
    missions = []
    for m in app.state.active_missions.values():
        if not m.get("waypoints"):
            continue
        missions.append({
            "mission_id": m.get("mission_id"),
            "mission_type": m.get("mission_type", "delivery"),
            "takeoff_profile": m.get("takeoff_profile", "vertical"),
            "status": m.get("status"),
            "waypoints": m.get("waypoints", []),
        })
    return {"missions": missions, "trails": app.state.drone_trails}
