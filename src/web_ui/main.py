# src/web_ui/main.py
import json, asyncio, yaml
from pathlib import Path
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import paho.mqtt.client as mqtt

APP_ROOT = Path(__file__).parents[1]
SIM_CFG = APP_ROOT / "simulator" / "config.yaml"

app = FastAPI(title="Drone System Dashboard")
app.mount("/static", StaticFiles(directory="src/web_ui/static"), name="static")

@app.get("/")
async def index():
    return FileResponse("src/web_ui/static/index.html")

# ---- settings helpers ----
def read_cfg():
    return yaml.safe_load(SIM_CFG.read_text())

def write_cfg(cfg):
    SIM_CFG.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))

@app.get("/api/settings")
async def get_settings():
    cfg = read_cfg()
    return {"base": cfg.get("base"), "drone_count": len(cfg.get("drones", []))}

@app.post("/api/settings")
async def update_settings(body: dict):
    """
    body: {"base": {"lat":..,"lon":..}, "drone_count": N, "mavsdk_port_start":14540}
    """
    cfg = read_cfg()
    if "base" in body:
        cfg["base"] = body["base"]

    if "drone_count" in body:
        start = int(body.get("mavsdk_port_start", 14540))
        # перегенерируем список дронов с последовательными портами
        cfg["drones"] = [{
            "id": f"sim_drone_{i+1}",
            "px4_path": "./PX4-Autopilot",
            "world": "none",
            "mavsdk_port": start + i,
            "mqtt_prefix": f"drone/sim_drone_{i+1}",
            "speedup": 1.0
        } for i in range(int(body["drone_count"]))]

    write_cfg(cfg)
    return {"status": "ok", "drones": cfg["drones"]}

# ---- drones list for UI ----
@app.get("/api/drones")
async def get_drones():
    cfg = read_cfg()
    return {"drones": [{"id": d["id"], "port": d["mavsdk_port"]} for d in cfg.get("drones", [])]}

# ---- MQTT → WebSocket telemetry fanout ----
telemetry_clients = set()
@app.on_event("startup")
async def startup():
    loop = asyncio.get_event_loop()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="web-ui")
    client.connect("localhost", 1883, 60)

    def on_message(client, userdata, msg):
        data = msg.payload.decode()
        # шлём всем подписчикам “как есть”
        for ws in telemetry_clients.copy():
            asyncio.run_coroutine_threadsafe(ws.send_text(data), loop)

    client.on_message = on_message
    client.subscribe("drone/+/telemetry/position")
    client.loop_start()

@app.websocket("/ws/telemetry")
async def telemetry_ws(websocket: WebSocket):
    await websocket.accept()
    telemetry_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        telemetry_clients.discard(websocket)

# ---- demo orders API (как было) ----
orders = []
@app.post("/api/orders")
async def create_order(order: dict):
    orders.append(order)
    return {"status": "created", "order": order}

@app.get("/api/orders")
async def get_orders():
    return {"orders": orders}
