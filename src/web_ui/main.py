from __future__ import annotations
import json
import asyncio
import yaml
from pathlib import Path
from typing import Any, Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from starlette.datastructures import State
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from drone_core.config.settings import Settings
from drone_core.infra.repositories.fleet_mem import FleetMem
from drone_core.infra.repositories.missions_mem import MissionsMem
from drone_core.domain.models import Order, LLA
from drone_core.infra.messaging.mqtt_bus import MqttBus

from .launcher import router as launcher_router
from .real import router as real_router
from . import auth as authmod
from .control import ControlAuthority

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

# Арбитраж управления: одновременно командовать может только один клиент.
control = ControlAuthority(ttl=float(settings.CONTROL_TTL_SEC))

# Отозванные сессии (logout). Stateless-токен иначе валиден до exp — это даёт
# серверную инвалидацию по logout. В памяти процесса; при рестарте сбрасывается.
revoked_sids: set[str] = set()

# Публичные пути (без авторизации): страница входа, сам логин, статика.
_PUBLIC_PREFIXES = ("/login", "/api/login", "/static/", "/favicon")


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or any(path == p or path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)
    payload = authmod.operator_from_cookies(request.cookies, settings.SESSION_SECRET)
    if payload is None or payload.get("sid") in revoked_sids:
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(url=f"/login?next={path}", status_code=302)
    request.state.auth = payload  # {op, sid, exp}
    return await call_next(request)


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
    app.state.active_missions = {}  # {mission_id: {...}} — активные (с авто-чисткой терминальных)
    # Журнал ВСЕХ миссий за сессию sim'а (вкл. завершённые) — НЕ чистится по таймеру,
    # чтобы вкладка "Session Missions" переживала перезагрузку страницы. Сбрасывается
    # только при (пере)запуске симуляции (reset_sim_session_state).
    app.state.session_missions = {}  # {mission_id: {...}}
    # Буфер траекторий: {drone_id: [[lon,lat,alt], ...]} — чтобы трейлы переживали
    # перезагрузку страницы (фронт восстанавливает их из снапшота при загрузке).
    app.state.drone_trails = {}
    _TRAIL_MAX = 600

    def _update_mission(mid: str, **fields):
        import time as _t
        now_ms = int(_t.time() * 1000)
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

        # Зеркалим в session-журнал (не чистится) — источник для вкладки Missions
        # после перезагрузки страницы. first_seen фиксируем один раз.
        sm = app.state.session_missions.setdefault(mid, {
            "mission_id": mid,
            "first_seen": now_ms,
            "mission_type": "delivery",
            "takeoff_profile": "vertical",
            "status": "PLANNED",
            "vehicle_id": None,
            "progress_current": 0,
            "progress_total": 0,
            "notes": None,
            "wp_count": None,
        })
        for k, v in fields.items():
            if v is None:
                continue
            if k == "waypoints":
                sm["wp_count"] = len(v) if isinstance(v, list) else sm.get("wp_count")
            else:
                sm[k] = v
        sm["updated"] = now_ms

        # Терминальную миссию (выполнена/отменена/ошибка) держим в АКТИВНОМ списке ещё
        # 20с (UI показывает её как COMPLETED/100% / failed), потом убираем — линия
        # таймлайна исчезает. В session-журнале она остаётся навсегда (до reset sim'а).
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


# === Auth: вход по общему код-паролю ===
@app.get("/login")
async def login_page():
    return FileResponse(str(APP_ROOT / "web_ui" / "static" / "login.html"))


@app.post("/api/login")
async def api_login(body: Dict[str, Any], response: Response):
    code = str((body or {}).get("code") or "")
    callsign = str((body or {}).get("callsign") or "").strip() or "operator"
    import hmac as _hmac
    if not _hmac.compare_digest(code, settings.ACCESS_CODE):
        return JSONResponse({"ok": False, "error": "bad_code"}, status_code=401)
    token, _sid = authmod.make_session_token(callsign, settings.SESSION_SECRET, settings.SESSION_TTL_HOURS)
    response = JSONResponse({"ok": True, "operator": callsign})
    response.set_cookie(
        authmod.COOKIE_NAME, token,
        httponly=True, samesite="lax", secure=bool(settings.COOKIE_SECURE),
        max_age=settings.SESSION_TTL_HOURS * 3600, path="/",
    )
    return response


@app.post("/api/logout")
async def api_logout(request: Request):
    # снять управление за этой сессией, чтобы борт не залип
    sid = (getattr(request.state, "auth", {}) or {}).get("sid")
    if sid:
        revoked_sids.add(sid)  # инвалидируем токен этой сессии
        for scope, lease in list(control.snapshot().items()):
            if lease.get("sid") == sid:
                control.release(scope, sid)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(authmod.COOKIE_NAME, path="/")
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    a = getattr(request.state, "auth", {}) or {}
    return {"operator": a.get("op"), "sid": a.get("sid"), "iat": a.get("iat")}


# === Арбитраж управления ===
def _auth_of(request: Request) -> Dict[str, Any]:
    return getattr(request.state, "auth", {}) or {}


@app.get("/api/control")
async def api_control_state(request: Request):
    a = _auth_of(request)
    sid = a.get("sid")
    leases = control.snapshot()
    out = {}
    for scope, l in leases.items():
        out[scope] = {"operator": l["operator"], "mine": l["sid"] == sid, "expires": l["expires"]}
    return {"leases": out, "me": {"operator": a.get("op"), "sid": sid}}


@app.post("/api/control/acquire")
async def api_control_acquire(request: Request, body: Optional[Dict[str, Any]] = None):
    a = _auth_of(request)
    scope = str((body or {}).get("scope") or "system")
    ok, info = control.acquire(scope, a.get("op", "operator"), a.get("sid", ""))
    if ok:
        return {"ok": True, "scope": scope, "lease": {"operator": info["operator"], "mine": True}}
    return JSONResponse(
        {"ok": False, "error": "held", "scope": scope, "holder": info["operator"]},
        status_code=409,
    )


@app.post("/api/control/heartbeat")
async def api_control_heartbeat(request: Request, body: Optional[Dict[str, Any]] = None):
    a = _auth_of(request)
    scope = str((body or {}).get("scope") or "system")
    ok = control.heartbeat(scope, a.get("sid", ""))
    return {"ok": ok, "scope": scope}


@app.post("/api/control/release")
async def api_control_release(request: Request, body: Optional[Dict[str, Any]] = None):
    a = _auth_of(request)
    scope = str((body or {}).get("scope") or "system")
    control.release(scope, a.get("sid", ""))
    return {"ok": True, "scope": scope}


@app.post("/api/control/takeover")
async def api_control_takeover(request: Request, body: Optional[Dict[str, Any]] = None):
    a = _auth_of(request)
    scope = str((body or {}).get("scope") or "system")
    prev = control.takeover(scope, a.get("op", "operator"), a.get("sid", ""))
    print(f"[CONTROL] takeover scope={scope} by={a.get('op')} prev={prev.get('operator') if prev else None}")
    return {"ok": True, "scope": scope, "previous": (prev or {}).get("operator")}


# === Ручное управление дроном (guided) — под арбитражем ===
_MANUAL_ACTIONS = {"arm", "disarm", "takeoff", "land", "rtl", "goto", "hold"}


@app.post("/api/drone/{veh_id}/command")
async def api_drone_command(veh_id: str, body: Dict[str, Any], request: Request):
    """Ручная команда борту. Требует держателя управления (scope "system").
    Безопасный guided-набор: arm/takeoff/land/rtl/goto/hold (без сырого стика)."""
    a = _auth_of(request)
    if not control.is_holder("system", a.get("sid", "")):
        holder = control.get("system")
        return JSONResponse(
            {"error": "no_control", "holder": (holder or {}).get("operator"),
             "message": "Take control before commanding a drone."},
            status_code=403,
        )
    action = str((body or {}).get("action") or "").lower()
    if action not in _MANUAL_ACTIONS:
        return JSONResponse({"error": "bad_action", "allowed": sorted(_MANUAL_ACTIONS)}, status_code=400)

    veh = veh_id if str(veh_id).startswith("veh_") else f"veh_{veh_id}"
    # hold = задержаться на месте: goto на текущую позицию борта (если известна)
    if action == "hold":
        d = app.state.active_drones.get(veh) or app.state.active_drones.get(veh_id) or {}
        payload = {"lat": d.get("lat"), "lon": d.get("lon"), "alt": d.get("alt") or 20.0}
        topic = f"cmd/{veh}/goto"
    elif action == "goto":
        try:
            payload = {"lat": float(body["lat"]), "lon": float(body["lon"]),
                       "alt": float(body.get("alt") or 30.0)}
        except (KeyError, TypeError, ValueError):
            return JSONResponse({"error": "goto requires lat,lon,alt"}, status_code=400)
        topic = f"cmd/{veh}/goto"
    else:
        topic = f"cmd/{veh}/{action}"
        payload = {}

    bus.publish(topic, payload, qos=1)
    print(f"[MANUAL] {a.get('op')} → {topic} {payload}")
    return {"ok": True, "veh": veh, "action": action}


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
async def api_orders(body: Dict[str, Any], request: Request):
    """Создаёт заказ, публикует его в MQTT → orchestrator.

    Требует, чтобы вызывающий держал управление (scope "system") — арбитраж не
    даёт двум операторам диспетчеризовать одновременно.

    Дополнительные поля (drone_id, priority, mission_type, cruise_alt_m,
    cruise_speed_mps, takeoff_alt_m, auto_rth, notes, и расширенные
    waypoints/loop_count/orbit_radius_m/pattern/sector_*) пробрасываются
    в payload MQTT под ключом `extras` — orchestrator может их учитывать
    либо игнорировать."""
    a = _auth_of(request)
    if not control.is_holder("system", a.get("sid", "")):
        holder = control.get("system")
        return JSONResponse(
            {"error": "no_control",
             "holder": (holder or {}).get("operator"),
             "message": "Take control before dispatching missions."},
            status_code=403,
        )
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
    # WS авторизуется той же cookie (уходит при same-origin handshake).
    payload = authmod.operator_from_cookies(websocket.cookies, settings.SESSION_SECRET)
    if payload is None or payload.get("sid") in revoked_sids:
        await websocket.close(code=1008)  # policy violation
        return
    await websocket.accept()
    telemetry_clients.add(websocket)
    print(f"🌐 WebSocket клиент подключен (op={payload.get('op')})")

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
    """Снапшот сессии для восстановления после перезагрузки страницы:
    - missions: активные миссии с waypoints — для перерисовки маршрутов на карте;
    - session_missions: полный журнал миссий сессии — для вкладки Session Missions
      (переживает reload, т.к. хранится на сервере и не чистится по таймеру);
    - trails: траектории дронов.
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
    return {
        "missions": missions,
        "session_missions": list(app.state.session_missions.values()),
        "trails": app.state.drone_trails,
    }


def reset_sim_session_state() -> None:
    """Полный сброс эфемерного состояния флота/миссий при (пере)запуске симуляции.

    Дроны, их телеметрия, активные и session-миссии, трейлы — обнуляются, чтобы
    новый sim-кластер не наследовал «призраков» прошлого запуска (старые борта,
    зависшие миссии, чужие траектории). Пользовательские рисунки (KOZ/POI/маршруты)
    живут на клиенте в localStorage и здесь НЕ затрагиваются."""
    for attr in ("active_drones", "active_missions", "drone_trails", "session_missions"):
        d = getattr(app.state, attr, None)
        if isinstance(d, dict):
            d.clear()
    print("[UI] sim session state reset (drones/missions/trails cleared)")
