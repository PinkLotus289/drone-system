# src/web_ui/main.py
from __future__ import annotations
import json, asyncio, yaml
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from drone_core.config.settings import Settings
from drone_core.infra.repositories import make_repos
from drone_core.domain.models import Order, LLA
from drone_core.infra.messaging.mqtt_bus import MqttBus

APP_ROOT = Path(__file__).parents[1]
SIM_CFG = APP_ROOT / "simulator" / "config.yaml"

app = FastAPI(title="Drone System Dashboard")
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "web_ui" / "static")), name="static")

settings = Settings()
bus = MqttBus(settings.MQTT_URL)
fleet_repo, missions_repo = make_repos()

def read_cfg() -> Dict[str, Any]:
    with open(SIM_CFG, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

@app.get("/")
async def index():
    return FileResponse(str(APP_ROOT / "web_ui" / "static" / "index.html"))

@app.get("/api/base")
async def api_base():
    cfg = read_cfg()
    base = cfg.get("base", {"lat": 43.07470, "lon": -89.38420})
    return base

@app.get("/api/drones")
async def api_drones():
    drones = await fleet_repo.list_all()
    return [d.dict() for d in drones]

@app.get("/api/missions")
async def api_missions():
    ms = await missions_repo.list_active()
    return [m.dict() for m in ms]

@app.post("/api/orders")
async def api_orders(body: Dict[str, Any]):
    cfg = read_cfg()
    base_cfg = cfg.get("base", {"lat": 43.07470, "lon": -89.38420})
    base = LLA(lat=float(base_cfg["lat"]), lon=float(base_cfg["lon"]), alt=60.0)

    # поддержка обоих форматов
    addr1 = body.get("addr1") or {
        "lat": body.get("pickup_lat"),
        "lon": body.get("pickup_lon"),
        "alt": 60.0
    }
    addr2 = body.get("addr2") or {
        "lat": body.get("drop_lat"),
        "lon": body.get("drop_lon"),
        "alt": 60.0
    }

    payload_kg = float(body.get("payload_kg", 2.0))
    priority = body.get("priority", "normal")

    # проверим, что координаты есть
    if not addr1["lat"] or not addr1["lon"] or not addr2["lat"] or not addr2["lon"]:
        return {"error": "missing coordinates"}

    order = Order(
        base=base,
        addr1=LLA(**addr1),
        addr2=LLA(**addr2),
        payload_kg=payload_kg,
        priority=priority,
    )

    bus.publish("orders/new", order.dict())
    return {"status": "ok", "order_id": order.id}

# ---- MQTT → WebSocket fanout ----
telemetry_clients: set[WebSocket] = set()

@app.on_event("startup")
async def _startup():
    bus.start()

@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    telemetry_clients.add(ws)

    @app.websocket("/ws")
    async def ws(ws: WebSocket):
        await ws.accept()
        telemetry_clients.add(ws)

        # локальный обработчик MQTT, вещает всем WS-клиентам
        def _mqtt_handler(message):
            topic = message.topic
            try:
                if isinstance(message.payload, bytes):
                    payload = json.loads(message.payload.decode("utf-8"))
                elif isinstance(message.payload, str):
                    payload = json.loads(message.payload) if message.payload.strip().startswith(
                        "{") else message.payload
                else:
                    payload = message.payload
            except Exception:
                payload = {}

            # создаём сообщение для UI
            data = {"topic": topic, "payload": payload}

            # 1️⃣ fleet/active → отрисовать дрон
            if topic == "fleet/active":
                data["type"] = "drone_active"

            # 2️⃣ telem/+/+ → обновление телеметрии
            elif topic.startswith("telem/"):
                data["type"] = "telemetry_update"

            # 3️⃣ mission/+/planned → маршрут
            elif topic.endswith("/planned"):
                data["type"] = "mission_planned"

            # 4️⃣ mission/+/status → статус миссии
            elif topic.endswith("/status"):
                data["type"] = "mission_status"

            # 5️⃣ mission/+/assigned → назначение дрона
            elif topic.endswith("/assigned"):
                data["type"] = "mission_assigned"

            # вещаем всем активным WS
            dead = []
            for c in list(telemetry_clients):
                try:
                    asyncio.run_coroutine_threadsafe(c.send_text(json.dumps(data)), asyncio.get_event_loop())
                except Exception:
                    dead.append(c)
            for d in dead:
                telemetry_clients.discard(d)

        # подписки для UI
        bus.subscribe("fleet/active", _mqtt_handler, qos=1)
        bus.subscribe("telem/+/+", _mqtt_handler, qos=0)
        bus.subscribe("mission/+/planned", _mqtt_handler, qos=1)
        bus.subscribe("mission/+/status", _mqtt_handler, qos=1)
        bus.subscribe("mission/+/assigned", _mqtt_handler, qos=1)

        try:
            while True:
                await ws.receive_text()  # держим соединение
        except WebSocketDisconnect:
            pass
        finally:
            telemetry_clients.discard(ws)
