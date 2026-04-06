#!/usr/bin/env python3
import asyncio
import inspect
import json
import logging
import os
import time
from pathlib import Path
import sys
import yaml

# --- путь к проекту ---
sys.path.append(str(Path(__file__).resolve().parents[1]))

from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan
from drone_core.config.settings import Settings
from drone_core.infra.messaging.mqtt_bus import MqttBus

# --- логирование ---
log = logging.getLogger("mavsdk-bridge")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

MISSION_ITEM_SIGNATURE = str(inspect.signature(MissionItem.__init__))
_MISSION_ITEM_SIGNATURE_LOGGED = False


def _map_kind_to_vehicle_action(kind: str) -> MissionItem.VehicleAction:
    normalized_kind = str(kind or "NAV").upper()
    kind_to_action = {
        "NAV": MissionItem.VehicleAction.NONE,
        "WAYPOINT": MissionItem.VehicleAction.NONE,
        "TAKEOFF": MissionItem.VehicleAction.TAKEOFF,
        "LAND": MissionItem.VehicleAction.LAND,
    }
    if normalized_kind not in kind_to_action:
        log.warning("Неизвестный kind='%s', использую VehicleAction.NONE", normalized_kind)
    return kind_to_action.get(normalized_kind, MissionItem.VehicleAction.NONE)


def _publish_mission_status(bus: MqttBus, mission_id: str, vehicle_name: str, status: str, error: str | None = None) -> None:
    topic = f"mission/{mission_id}/status"
    payload = {
        "mission_id": mission_id,
        "vehicle_id": vehicle_name,
        "status": status,
        "ts": time.time(),
    }
    if error:
        payload["error"] = error
    bus.publish(topic, payload, qos=1)
    log.info("[%s] MQTT status published: %s -> %s", vehicle_name, topic, status)


def _publish_mission_event(
    bus: MqttBus,
    mission_id: str,
    vehicle_name: str,
    event: str,
    details: dict | None = None,
) -> None:
    topic = f"mission/{mission_id}/events"
    payload = {
        "mission_id": mission_id,
        "vehicle_id": vehicle_name,
        "event": event,
        "ts": time.time(),
    }
    if details:
        payload["details"] = details
    bus.publish(topic, payload, qos=1)
    log.info("[%s] MQTT event published: %s -> %s", vehicle_name, topic, event)


def _set_state(state_ctx: dict, vehicle_name: str, new_state: str, reason: str = "") -> None:
    old_state = state_ctx.get("state", "idle")
    if old_state == new_state:
        return
    suffix = f" ({reason})" if reason else ""
    log.info("[%s] [STATE] %s -> %s%s", vehicle_name, old_state, new_state, suffix)
    state_ctx["state"] = new_state

# =====================================================
#  Подключение к PX4
# =====================================================
async def connect_system(connection_url: str) -> System:
    sys = System()
    log.info(f"🔌 Подключаюсь к PX4 через {connection_url} ...")
    await sys.connect(system_address=connection_url)

    async for state in sys.core.connection_state():
        if state.is_connected:
            log.info(f"✅ MAVSDK connected to {connection_url}")
            return sys

    raise RuntimeError(f"❌ Не удалось подключиться к PX4: {connection_url}")


# =====================================================
#  Обработка MQTT команд
# =====================================================
async def handle_command(msg, sys: System, name: str, bus: MqttBus, state_ctx: dict):
    topic = msg.topic
    cmd = topic.split("/")[-1]
    log.info(f"[{name}] ⚡ MQTT команда получена: topic={topic}")

    # --- парсим payload ---
    try:
        if isinstance(msg.payload, (bytes, bytearray)):
            payload = json.loads(msg.payload.decode("utf-8"))
        elif isinstance(msg.payload, str):
            payload = json.loads(msg.payload)
        else:
            payload = msg.payload or {}
    except Exception as e:
        log.error(f"[{name}] Ошибка парсинга payload команды: {e}")
        return

    log.info(f"[{name}] 📡 cmd={cmd}, payload={payload}")

    # --- выполнение команд ---
    try:
        if cmd == "arm":
            _set_state(state_ctx, name, "arming")
            await sys.action.arm()
            _set_state(state_ctx, name, "armed")
            log.info(f"[{name}] ✅ Armed")

        elif cmd == "takeoff":
            await sys.action.takeoff()
            log.info(f"[{name}] ✈️ Взлёт")

        elif cmd == "goto":
            lat, lon, alt = payload["lat"], payload["lon"], payload["alt"]
            await sys.action.goto_location(lat, lon, alt, 0)
            log.info(f"[{name}] 🧭 GOTO → ({lat}, {lon}, {alt})")

        elif cmd == "land":
            mission_id = str(payload.get("mission_id") or state_ctx.get("mission_id") or "unknown")
            log.info(f"[{name}] [EMERGENCY] land requested, mission_id={mission_id}")
            _set_state(state_ctx, name, "landing")
            await sys.action.land()
            log.info(f"[{name}] 🛬 Посадка")
            _publish_mission_event(bus, mission_id, name, "EMERGENCY_LAND_REQUESTED")

        elif cmd in ("rtl", "return_to_launch"):
            mission_id = str(payload.get("mission_id") or state_ctx.get("mission_id") or "unknown")
            log.info(f"[{name}] [EMERGENCY] rtl requested, mission_id={mission_id}")
            _set_state(state_ctx, name, "rtl")
            await sys.action.return_to_launch()
            log.info(f"[{name}] 🏠 RTL")
            _publish_mission_event(bus, mission_id, name, "EMERGENCY_RTL_REQUESTED")

        elif cmd == "mission.upload":
            global _MISSION_ITEM_SIGNATURE_LOGGED
            waypoints = payload.get("waypoints", [])
            mission_id = str(payload.get("mission_id") or "unknown")
            state_ctx["mission_id"] = mission_id
            log.info(f"[{name}] [MISSION] upload begin mission_id={mission_id} points={len(waypoints)}")
            if not waypoints:
                log.warning(f"[{name}] ⚠️ Пустая миссия в mission.upload")
                _publish_mission_status(
                    bus,
                    mission_id,
                    name,
                    "UPLOAD_FAILED",
                    error="waypoints is empty",
                )
                log.info(f"[{name}] [MISSION] upload result=UPLOAD_FAILED mission_id={mission_id}")
                _set_state(state_ctx, name, "error", reason="empty mission")
                return

            if not _MISSION_ITEM_SIGNATURE_LOGGED:
                log.info("MissionItem signature detected: %s", MISSION_ITEM_SIGNATURE)
                _MISSION_ITEM_SIGNATURE_LOGGED = True

            mission_items = []
            for idx, wp in enumerate(waypoints, start=1):
                pos = wp.get("pos", wp)
                lat = float(pos["lat"])
                lon = float(pos["lon"])
                alt = float(pos["alt"])
                hold_s = float(wp.get("hold_s") or 0.0)
                vehicle_action = _map_kind_to_vehicle_action(wp.get("kind", "NAV"))

                mission_items.append(
                    MissionItem(
                        latitude_deg=lat,
                        longitude_deg=lon,
                        relative_altitude_m=alt,
                        speed_m_s=5.0,
                        is_fly_through=True,
                        gimbal_pitch_deg=0.0,
                        gimbal_yaw_deg=0.0,
                        camera_action=MissionItem.CameraAction.NONE,
                        loiter_time_s=hold_s,
                        camera_photo_interval_s=0.0,
                        acceptance_radius_m=5.0,
                        yaw_deg=0.0,
                        camera_photo_distance_m=0.0,
                        vehicle_action=vehicle_action,
                    )
                )
                log.info(
                    "[%s] mission.upload wp#%d -> lat=%.7f lon=%.7f alt=%.2f hold_s=%.2f action=%s",
                    name,
                    idx,
                    lat,
                    lon,
                    alt,
                    hold_s,
                    vehicle_action,
                )

            plan = MissionPlan(mission_items)
            await sys.mission.upload_mission(plan)
            log.info(f"[{name}] 📦 Миссия загружена ({len(mission_items)} точек), mission_id={mission_id}")
            _publish_mission_status(bus, mission_id, name, "UPLOADED")
            log.info(f"[{name}] [MISSION] upload result=UPLOADED mission_id={mission_id}")
            _set_state(state_ctx, name, "mission_uploaded")

        elif cmd == "mission.start":
            mission_id = str(payload.get("mission_id") or "unknown")
            state_ctx["mission_id"] = mission_id
            log.info(f"[{name}] 📨 mission.start received, mission_id={mission_id}")
            log.info(f"[{name}] [MISSION] start begin mission_id={mission_id}")
            log.info(f"[{name}] 🚀 mission.start begin, mission_id={mission_id}")
            await sys.mission.start_mission()
            log.info(f"[{name}] ✅ mission.start success, mission_id={mission_id}")
            _publish_mission_status(bus, mission_id, name, "STARTED")
            log.info(f"[{name}] [MISSION] start result=STARTED mission_id={mission_id}")
            _set_state(state_ctx, name, "mission_running")

        elif cmd == "reroute.manual":
            mission_id = str(payload.get("mission_id") or state_ctx.get("mission_id") or "unknown")
            log.info(f"[{name}] [EMERGENCY] reroute.manual requested (stub), mission_id={mission_id}")
            _publish_mission_event(
                bus,
                mission_id,
                name,
                "REROUTE_MANUAL_REQUESTED_STUB",
                details={"note": "manual reroute is not implemented yet"},
            )

        else:
            log.warning(f"[{name}] ⚠️ Неизвестная команда: {cmd}")

    except Exception as e:
        log.error(f"[{name}] ❌ Ошибка при выполнении {cmd}: {e}")
        _set_state(state_ctx, name, "error", reason=f"{cmd} failed")
        if cmd == "mission.upload":
            mission_id = str(payload.get("mission_id") or "unknown")
            try:
                _publish_mission_status(bus, mission_id, name, "UPLOAD_FAILED", error=str(e))
                log.info(f"[{name}] [MISSION] upload result=UPLOAD_FAILED mission_id={mission_id}")
            except Exception as publish_err:
                log.error(f"[{name}] ❌ Не удалось опубликовать UPLOAD_FAILED: {publish_err}")
        elif cmd == "mission.start":
            mission_id = str(payload.get("mission_id") or "unknown")
            log.error(f"[{name}] ❌ mission.start fail, mission_id={mission_id}, error={e}")
            try:
                _publish_mission_status(bus, mission_id, name, "START_FAILED", error=str(e))
                log.info(f"[{name}] [MISSION] start result=START_FAILED mission_id={mission_id}")
            except Exception as publish_err:
                log.error(f"[{name}] ❌ Не удалось опубликовать START_FAILED: {publish_err}")


# =====================================================
#  Логика отдельного дрона
# =====================================================
async def run_for_drone(
    instance_id: str,
    connection_url: str,
    home_lat: float,
    home_lon: float,
    home_alt: float,
):
    name = f"veh_{instance_id}"
    settings = Settings()
    state_ctx = {"state": "idle", "mission_id": "unknown"}

    # отдельный MQTT клиент для каждого дрона
    bus = MqttBus(settings.MQTT_URL, client_id=f"mavsdk-{name}-{os.getpid()}")
    bus.start()

    sys = await connect_system(connection_url)

    # ждём GPS
    async for h in sys.telemetry.health():
        if h.is_global_position_ok and h.is_home_position_ok:
            break

    # первая публикация
    bus.publish("fleet/active", {
        "id": name,
        "name": name,
        "status": "IDLE",
        "lat": home_lat,
        "lon": home_lon,
        "alt": home_alt,
        "soc": 100.0,
    }, qos=1)
    log.info(f"[{name}] 👋 Объявился во fleet/active")
    _set_state(state_ctx, name, "idle", reason="bridge ready")

    # подписка на команды
    loop = asyncio.get_running_loop()

    def _on_command(message):
        asyncio.run_coroutine_threadsafe(handle_command(message, sys, name, bus, state_ctx), loop)

    bus.subscribe(
        f"cmd/{name}/#",
        _on_command,
        qos=1,
    )
    log.info(f"[{name}] 🔔 Подписан на cmd/{name}/#")

    # --- телеметрия позиции ---
    async def publish_position():
        try:
            async for pos in sys.telemetry.position():
                bus.publish(f"telem/{name}/pose", {
                    "lat": pos.latitude_deg,
                    "lon": pos.longitude_deg,
                    "alt": pos.relative_altitude_m,
                    "ts": time.time(),
                }, qos=0)
                await asyncio.sleep(5.0)
        except Exception as e:
            log.error(f"[{name}] ❌ Ошибка телеметрии позиции: {e}")

    # --- статус FLYING / IDLE ---
    async def monitor_in_air():
        last_status = None
        try:
            async for in_air in sys.telemetry.in_air():
                pos = await sys.telemetry.position().__anext__()
                if pos.relative_altitude_m < 1.0:
                    in_air = False
                status = "FLYING" if in_air else "IDLE"
                if status != last_status:
                    bus.publish("fleet/active", {
                        "id": name,
                        "name": name,
                        "status": status,
                        "lat": pos.latitude_deg,
                        "lon": pos.longitude_deg,
                        "alt": pos.relative_altitude_m,
                        "soc": 100.0,
                    }, qos=0)
                    last_status = status
                    if status == "IDLE" and state_ctx.get("state") in {
                        "landing", "rtl", "mission_running", "armed", "arming"
                    }:
                        _set_state(state_ctx, name, "landed", reason="telemetry in_air=False")
                        _set_state(state_ctx, name, "idle")
                await asyncio.sleep(1.0)
        except Exception as e:
            log.error(f"[{name}] ❌ Ошибка статуса in_air: {e}")

    try:
        await asyncio.gather(publish_position(), monitor_in_air())
    finally:
        bus.stop()


# =====================================================
#  Главная точка входа
# =====================================================
async def main_async():
    cfg_path = Path(__file__).resolve().parent / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())

    drones = cfg["drones"]
    sim_home = cfg["simulator"]["home"]
    home_lat, home_lon, home_alt = (
        sim_home["lat"],
        sim_home["lon"],
        sim_home.get("alt", 0.0),
    )

    tasks = []
    for d in drones:
        instance_id = str(d["id"])
        out_port = d["mavlink_out"]
        connection_url = f"udp://:{out_port}"
        tasks.append(asyncio.create_task(
            run_for_drone(instance_id, connection_url, home_lat, home_lon, home_alt)
        ))

    await asyncio.gather(*tasks)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
