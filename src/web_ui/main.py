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
import paho.mqtt.client as mqtt

from drone_core.config.settings import Settings
from drone_core.infra.repositories.fleet_mem import FleetMem
from drone_core.infra.repositories.missions_mem import MissionsMem
from drone_core.domain.models import Order, LLA
from drone_core.infra.messaging.mqtt_bus import MqttBus

# --- пути и настройки ---
APP_ROOT = Path(__file__).parents[1]
SIM_CFG = APP_ROOT / "simulator" / "config.yaml"

app = FastAPI(title="Drone System Dashboard")
app.state: State
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "web_ui" / "static")), name="static")

settings = Settings()
bus = MqttBus(settings.MQTT_URL, client_id="web-ui-bus")
fleet_repo = FleetMem()
missions_repo = MissionsMem()
telemetry_clients: set[WebSocket] = set()
active_drones: dict[str, dict] = {}

# --- MQTT клиент (для заказов) ---
mqtt_client = mqtt.Client(client_id="web-ui", protocol=mqtt.MQTTv311)
mqtt_client.connect("127.0.0.1", 1883)
mqtt_client.loop_start()


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

    # Сохраняем активных дронов в памяти
    app.state.active_drones = {}

    # --- обработчик сообщений MQTT ---
    def _mqtt_handler(message):
        try:
            payload = message.payload.decode() if isinstance(message.payload, bytes) else str(message.payload)
            data = json.loads(payload) if payload.strip().startswith("{") else {"raw": payload}
        except Exception:
            data = {}

        topic = message.topic
        msg = {"topic": topic, "payload": data}

        if topic == "fleet/active":
            msg["type"] = "drone_active"
            d = data
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
        elif topic.endswith("/planned"):
            msg["type"] = "mission_planned"
        elif topic.endswith("/status"):
            msg["type"] = "mission_status"
        elif topic.endswith("/assigned"):
            msg["type"] = "mission_assigned"

        async def _send_to_all():
            text = json.dumps(msg)
            for c in list(telemetry_clients):
                try:
                    await c.send_text(text)
                except Exception:
                    telemetry_clients.discard(c)

        # Передаём задачу в главный event loop
        main_loop.call_soon_threadsafe(asyncio.create_task, _send_to_all())

    # --- подписки на MQTT ---
    bus.subscribe("fleet/active", _mqtt_handler, qos=1)
    bus.subscribe("telem/+/+", _mqtt_handler, qos=0)
    bus.subscribe("mission/+/planned", _mqtt_handler, qos=1)
    bus.subscribe("mission/+/status", _mqtt_handler, qos=1)
    bus.subscribe("mission/+/assigned", _mqtt_handler, qos=1)

# === Маршруты API ===
@app.get("/")
async def index():
    return FileResponse(str(APP_ROOT / "web_ui" / "static" / "index.html"))


@app.get("/api/base")
async def api_base():
    cfg = read_cfg()
    return cfg.get("base", {"lat": 43.07470, "lon": -89.38420})


@app.get("/api/drones")
async def api_drones():
    """
    Возвращает список активных дронов, полученных по MQTT (fleet/active)
    """
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
        "drone_count": len((await fleet_repo.list_all()) or [])
    }


@app.post("/api/orders")
async def api_orders(body: Dict[str, Any]):
    """
    POST /api/orders — создаёт заказ, публикует его в MQTT → orchestrator
    """
    cfg = read_cfg()
    base_cfg = cfg.get("base", {"lat": 43.07470, "lon": -89.38420})
    base = LLA(lat=float(base_cfg["lat"]), lon=float(base_cfg["lon"]), alt=60.0)

    addr1 = body.get("from") or {"lat": body.get("pickup_lat"), "lon": body.get("pickup_lon"), "alt": 60.0}
    addr2 = body.get("to") or {"lat": body.get("drop_lat"), "lon": body.get("drop_lon"), "alt": 60.0}
    payload_kg = float(body.get("weight", 2.0))

    if not addr1["lat"] or not addr2["lat"]:
        return {"error": "Missing coordinates"}

    order = Order(base=base, addr1=LLA(**addr1), addr2=LLA(**addr2), payload_kg=payload_kg, priority="normal")
    bus.publish("orders/new", order.dict())
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
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        telemetry_clients.discard(websocket)
        print("❌ WebSocket отключен")
