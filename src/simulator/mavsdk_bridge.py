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
async def connect_system(connection_url: str, grpc_port: int = 50051) -> System:
    # Уникальный gRPC-порт для изоляции mavsdk_server между инстансами.
    sys = System(port=grpc_port)
    log.info(f"🔌 Подключаюсь к PX4 через {connection_url} (gRPC :{grpc_port}) ...")
    await sys.connect(system_address=connection_url)

    async for state in sys.core.connection_state():
        if state.is_connected:
            log.info(f"✅ MAVSDK connected to {connection_url}")
            return sys

    raise RuntimeError(f"❌ Не удалось подключиться к PX4: {connection_url}")


# =====================================================
#  Настройка параметров PX4-SITL для автономного полёта без RC
# =====================================================
async def setup_sitl_params(system: System, log_prefix: str) -> None:
    """
    Без этих параметров PX4 SITL по дефолту:
    - требует RC input и уходит в HOLD/Return при отсутствии RC,
    - авто-disarm через 10с если armed, но не взлетел.
    В итоге дрон входит в TAKEOFF, но моторы не крутятся — failsafe блокирует thrust.
    """
    int_params = {
        "COM_RC_IN_MODE": 4,       # 4 = RC input disabled
        "NAV_RCL_ACT": 0,          # RC loss failsafe disabled
        "NAV_DLL_ACT": 0,          # Datalink loss failsafe disabled
        "COM_RCL_EXCEPT": 7,       # Mission|Hold|Offboard исключены из RC-checks
        "COM_ARM_CHK_ESCS": 0,     # SIH не эмулирует ESC-feedback — отключаем проверку
        "CBRK_USB_CHK": 197848,    # circuit-breaker: не требовать USB (magic value PX4)
        "CBRK_SUPPLY_CHK": 894281, # circuit-breaker: не требовать power module
        "CBRK_IO_SAFETY": 22027,   # circuit-breaker: не требовать safety switch
        # Принудительно включаем sim-sensors: в SIH multi-instance airframe-default
        # `SENS_EN_BAROSIM=1` иногда не применяется, и PX4 падает в failsafe
        # "No valid data from Baro/Compass" → mc_pos_control "blind land".
        "SENS_EN_BAROSIM": 1,
        "SENS_EN_MAGSIM": 1,
        "SENS_EN_GPSSIM": 1,
    }
    float_params = {
        "COM_DISARM_PRFLT": -1.0,  # не авто-disarm если armed без takeoff
        "COM_DISARM_LAND": -1.0,   # не авто-disarm по land-detector (SIH даёт ложные landed)
        "MIS_TAKEOFF_ALT": 5.0,    # дефолтная высота takeoff если target не задан
        "MPC_TKO_SPEED": 1.5,      # скорость подъёма на takeoff
    }
    for pname, pval in int_params.items():
        try:
            await system.param.set_param_int(pname, pval)
            log.info(f"{log_prefix} ⚙️ {pname}={pval}")
        except Exception as e:
            log.warning(f"{log_prefix} ⚠️ set {pname}={pval} warn: {e!r}")
    for pname, pval in float_params.items():
        try:
            await system.param.set_param_float(pname, pval)
            log.info(f"{log_prefix} ⚙️ {pname}={pval}")
        except Exception as e:
            log.warning(f"{log_prefix} ⚠️ set {pname}={pval} warn: {e!r}")


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

            first_wp = waypoints[0]
            first_pos = first_wp.get("pos", first_wp)
            state_ctx["takeoff_alt_m"] = float(first_pos["alt"])
            state_ctx["first_is_takeoff"] = str(first_wp.get("kind", "NAV")).upper() == "TAKEOFF"

            mission_items = []
            uploaded_idx = 0
            for idx, wp in enumerate(waypoints, start=1):
                pos = wp.get("pos", wp)
                lat = float(pos["lat"])
                lon = float(pos["lon"])
                alt = float(pos["alt"])
                hold_s = float(wp.get("hold_s") or 0.0)
                kind = str(wp.get("kind", "NAV")).upper()

                # TAKEOFF-item в mission-списке PX4 часто ведёт себя непредсказуемо
                # (конфликт с action.takeoff, is_fly_through не имеет смысла для взлёта).
                # Вместо этого поднимаем дрон чистым action.takeoff() в mission.start,
                # а в PX4-миссию заливаем только NAV + LAND.
                if kind == "TAKEOFF":
                    log.info(
                        "[%s] mission.upload wp#%d SKIP (TAKEOFF handled via action.takeoff): "
                        "lat=%.7f lon=%.7f alt=%.2f",
                        name, idx, lat, lon, alt,
                    )
                    continue

                vehicle_action = _map_kind_to_vehicle_action(kind)
                is_fly_through = kind == "NAV"  # для LAND fly-to, не fly-through

                mission_items.append(
                    MissionItem(
                        latitude_deg=lat,
                        longitude_deg=lon,
                        relative_altitude_m=alt,
                        speed_m_s=5.0,
                        is_fly_through=is_fly_through,
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
                uploaded_idx += 1
                log.info(
                    "[%s] mission.upload wp#%d -> px4_idx=%d lat=%.7f lon=%.7f alt=%.2f "
                    "hold_s=%.2f action=%s fly_through=%s",
                    name, idx, uploaded_idx - 1, lat, lon, alt, hold_s, vehicle_action, is_fly_through,
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

            takeoff_alt = float(state_ctx.get("takeoff_alt_m", 15.0))

            # 1) Явный auto-takeoff (TAKEOFF item в миссию не заливается — см. mission.upload).
            try:
                await sys.action.set_takeoff_altitude(takeoff_alt)
            except Exception as set_alt_err:
                log.warning(f"[{name}] set_takeoff_altitude({takeoff_alt}) warn: {set_alt_err!r}")

            log.info(f"[{name}] 🛫 action.takeoff() target_alt={takeoff_alt:.1f}m")
            await sys.action.takeoff()

            # 2) Ждём, пока дрон оторвётся от земли (relative_altitude_m > 3m).
            async def _wait_airborne():
                async for pos in sys.telemetry.position():
                    if pos.relative_altitude_m >= 3.0:
                        log.info(f"[{name}] ✈️ airborne alt={pos.relative_altitude_m:.2f}m")
                        return
            try:
                await asyncio.wait_for(_wait_airborne(), timeout=30.0)
            except asyncio.TimeoutError:
                log.error(f"[{name}] ❌ timeout: дрон не поднялся выше 3м за 30с после takeoff")
                raise

            # 3) Запускаем миссию — PX4 переходит в AUTO.MISSION, летит на первый NAV (px4_idx=0).
            log.info(f"[{name}] 🚀 mission.start_mission(), mission_id={mission_id}")
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
        err_text = f"{e.__class__.__name__}: {e!r}"
        log.error(f"[{name}] ❌ Ошибка при выполнении {cmd}: {err_text}")
        _set_state(state_ctx, name, "error", reason=f"{cmd} failed")
        if cmd == "mission.upload":
            mission_id = str(payload.get("mission_id") or "unknown")
            try:
                _publish_mission_status(bus, mission_id, name, "UPLOAD_FAILED", error=err_text)
                log.info(f"[{name}] [MISSION] upload result=UPLOAD_FAILED mission_id={mission_id}")
            except Exception as publish_err:
                log.error(f"[{name}] ❌ Не удалось опубликовать UPLOAD_FAILED: {publish_err!r}")
        elif cmd == "mission.start":
            mission_id = str(payload.get("mission_id") or "unknown")
            log.error(f"[{name}] ❌ mission.start fail, mission_id={mission_id}, error={err_text}")
            try:
                _publish_mission_status(bus, mission_id, name, "START_FAILED", error=err_text)
                log.info(f"[{name}] [MISSION] start result=START_FAILED mission_id={mission_id}")
            except Exception as publish_err:
                log.error(f"[{name}] ❌ Не удалось опубликовать START_FAILED: {publish_err!r}")
        elif cmd == "arm":
            log.error(f"[{name}] ❌ arm fail error={err_text} — часто из-за pre-arm checks (GPS/IMU/level)")


# =====================================================
#  Логика отдельного дрона
# =====================================================
async def run_for_drone(
    instance_id: str,
    connection_url: str,
    home_lat: float,
    home_lon: float,
    home_alt: float,
    grpc_port: int = 50051,
):
    name = f"veh_{instance_id}"
    settings = Settings()
    state_ctx = {"state": "idle", "mission_id": "unknown"}

    # отдельный MQTT клиент для каждого дрона
    bus = MqttBus(settings.MQTT_URL, client_id=f"mavsdk-{name}-{os.getpid()}")
    bus.start()

    sys = await connect_system(connection_url, grpc_port=grpc_port)

    # Конфигурация PX4 SITL для автономного полёта без RC
    await setup_sitl_params(sys, f"[{name}]")

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

    # Общее состояние между корутинами (latest telemetry).
    telem_state = {
        "lat": home_lat,
        "lon": home_lon,
        "alt_rel": 0.0,
        "alt_abs": 0.0,
        "in_air": False,
        "armed": False,
        "mission_current": 0,
        "mission_total": 0,
        "ts": 0.0,
    }

    # --- телеметрия позиции ---
    # ВАЖНО: не ставим asyncio.sleep внутри async for — иначе MAVSDK backpressure-stream
    # накапливает буфер и возвращает stale значения. Consume-им всё, rate-лимитим публикацию.
    async def publish_position():
        last_publish = 0.0
        last_log = 0.0
        try:
            async for pos in sys.telemetry.position():
                now = time.time()
                telem_state["lat"] = pos.latitude_deg
                telem_state["lon"] = pos.longitude_deg
                telem_state["alt_rel"] = pos.relative_altitude_m
                telem_state["alt_abs"] = pos.absolute_altitude_m
                telem_state["ts"] = now

                if now - last_publish >= 0.25:  # 4 Hz max в MQTT
                    bus.publish(f"telem/{name}/pose", {
                        "lat": pos.latitude_deg,
                        "lon": pos.longitude_deg,
                        "alt": pos.relative_altitude_m,
                        "ts": now,
                    }, qos=0)
                    last_publish = now

                if now - last_log >= 2.0:
                    log.info(
                        f"[{name}] [POS] lat={pos.latitude_deg:.7f} lon={pos.longitude_deg:.7f} "
                        f"alt_rel={pos.relative_altitude_m:.2f}m alt_abs={pos.absolute_altitude_m:.1f}m"
                    )
                    last_log = now
        except Exception as e:
            log.error(f"[{name}] ❌ Ошибка телеметрии позиции: {e!r}")

    # --- режим полёта (MANUAL/ALTITUDE/OFFBOARD/MISSION/HOLD/RETURN/LAND...) ---
    async def log_flight_mode():
        try:
            last = None
            async for mode in sys.telemetry.flight_mode():
                if mode != last:
                    log.info(f"[{name}] [MODE] flight_mode={mode}")
                    last = mode
        except Exception as e:
            log.error(f"[{name}] ❌ Ошибка телеметрии flight_mode: {e!r}")

    # --- armed/disarmed ---
    async def log_armed():
        try:
            last = None
            async for armed in sys.telemetry.armed():
                telem_state["armed"] = armed
                if armed != last:
                    log.info(f"[{name}] [ARM] armed={armed}")
                    last = armed
        except Exception as e:
            log.error(f"[{name}] ❌ Ошибка телеметрии armed: {e!r}")

    # --- прогресс миссии: current/total wp + MQTT publish ---
    async def log_mission_progress():
        try:
            async for p in sys.mission.mission_progress():
                mid = str(state_ctx.get("mission_id") or "unknown")
                telem_state["mission_current"] = int(p.current)
                telem_state["mission_total"] = int(p.total)
                log.info(f"[{name}] [MIS] progress={p.current}/{p.total}")
                bus.publish(f"mission/{mid}/progress", {
                    "mission_id": mid,
                    "vehicle_id": name,
                    "current": int(p.current),
                    "total": int(p.total),
                    "ts": time.time(),
                }, qos=0)
        except Exception as e:
            log.error(f"[{name}] ❌ Ошибка телеметрии mission_progress: {e!r}")

    # --- actuator outputs: реально ли PX4 крутит моторы ---
    async def log_actuators():
        try:
            last_logged = 0.0
            async for ao in sys.telemetry.actuator_output_status():
                now = time.time()
                if now - last_logged < 2.0:
                    continue
                outputs = list(getattr(ao, "actuator", []) or [])
                first4 = [round(float(v), 3) for v in outputs[:4]]
                log.info(f"[{name}] [ACT] motors[0:4]={first4} active={getattr(ao, 'active', '?')}")
                last_logged = now
        except Exception as e:
            log.error(f"[{name}] ❌ Ошибка телеметрии actuator_output_status: {e!r}")

    # --- in_air тракер (для статуса FLYING/IDLE) ---
    async def track_in_air():
        try:
            async for in_air in sys.telemetry.in_air():
                telem_state["in_air"] = in_air
        except Exception as e:
            log.error(f"[{name}] ❌ Ошибка телеметрии in_air: {e!r}")

    # --- периодическая публикация fleet/active + закрытие миссии по mission_progress ---
    async def publish_fleet_active():
        last_status = None
        mission_completed_published = False  # чтобы не шлять COMPLETED повторно
        try:
            while True:
                flying = bool(telem_state["in_air"]) and telem_state["alt_rel"] > 1.0
                status = "FLYING" if flying else "IDLE"

                bus.publish("fleet/active", {
                    "id": name,
                    "name": name,
                    "status": status,
                    "lat": telem_state["lat"],
                    "lon": telem_state["lon"],
                    "alt": telem_state["alt_rel"],
                    "soc": 100.0,
                }, qos=0)

                if status != last_status:
                    log.info(f"[{name}] [STATUS] {last_status} -> {status}")
                    last_status = status

                # Критерий завершения миссии: PX4 прошёл ВСЕ waypoints (current == total)
                # и дрон приземлился (не in_air, alt<1м). Armed-состояние не проверяем:
                # COM_DISARM_LAND=-1 отключает авто-disarm PX4, поэтому после landing
                # дрон остаётся armed — мы disarm-им его явно ниже.
                cur = telem_state["mission_current"]
                tot = telem_state["mission_total"]
                mid = str(state_ctx.get("mission_id") or "unknown")
                mission_active = (
                    mid != "unknown"
                    and state_ctx.get("state") in {"mission_running", "landing", "rtl", "armed"}
                )
                if (
                    mission_active
                    and not mission_completed_published
                    and tot > 0
                    and cur >= tot  # все waypoints исполнены
                    and not bool(telem_state["in_air"])
                    and telem_state["alt_rel"] < 1.0
                ):
                    log.info(
                        f"[{name}] 🏁 mission {mid} COMPLETED "
                        f"(progress={cur}/{tot}, landed)"
                    )
                    # Явный disarm после завершения миссии — COM_DISARM_LAND=-1
                    # держит моторы включёнными иначе.
                    try:
                        if bool(telem_state.get("armed")):
                            await sys.action.disarm()
                            log.info(f"[{name}] disarmed after mission")
                    except Exception as e:
                        log.warning(f"[{name}] disarm-after-mission warn: {e!r}")
                    _publish_mission_status(bus, mid, name, "COMPLETED")
                    _set_state(state_ctx, name, "landed", reason="mission finished")
                    _set_state(state_ctx, name, "idle")
                    state_ctx["mission_id"] = "unknown"
                    state_ctx["takeoff_alt_m"] = 0.0
                    state_ctx["first_is_takeoff"] = False
                    telem_state["mission_current"] = 0
                    telem_state["mission_total"] = 0
                    mission_completed_published = True

                # Готов к следующей миссии — сбрасываем флаг после перехода в idle.
                if mid == "unknown" and state_ctx.get("state") == "idle":
                    mission_completed_published = False

                await asyncio.sleep(1.0)
        except Exception as e:
            log.error(f"[{name}] ❌ Ошибка publish_fleet_active: {e!r}")

    try:
        await asyncio.gather(
            publish_position(),
            track_in_air(),
            publish_fleet_active(),
            log_flight_mode(),
            log_armed(),
            log_mission_progress(),
            log_actuators(),
        )
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

    # Режим одного дрона: переменная DRONE_ID выбирает целевой борт из конфига.
    # Нужен для multi-drone, т.к. MAVSDK-Python в одном Python-процессе не умеет
    # корректно изолировать два System() (mavsdk_server'ы «слипают» target system).
    # run_system.py запускает по одному bridge-процессу на дрон.
    drone_id_env = os.environ.get("DRONE_ID")
    if drone_id_env is not None:
        drones = [d for d in drones if str(d["id"]) == str(drone_id_env)]
        if not drones:
            raise RuntimeError(f"DRONE_ID={drone_id_env} не найден в config.yaml")

    tasks = []
    for d in drones:
        instance_id = str(d["id"])
        out_port = d["mavlink_out"]
        connection_url = f"udp://:{out_port}"
        # В single-drone-процессе все равно передаём уникальный gRPC-порт —
        # если bridge-процессов несколько, mavsdk_server не конкурируют.
        grpc_port = 50151 + int(d["id"])
        tasks.append(asyncio.create_task(
            run_for_drone(instance_id, connection_url, home_lat, home_lon, home_alt, grpc_port)
        ))

    await asyncio.gather(*tasks)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
