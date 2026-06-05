#!/usr/bin/env python3
import asyncio
import inspect
import json
import logging
import math
import os
import signal
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

# aiogrpc дублирует "Socket closed" исключения в свой логгер при остановке
# mavsdk_server — это шум, а не реальная ошибка. Bridge сам логирует те же
# исключения через свои try/except (с большим контекстом). Глушим.
logging.getLogger("aiogrpc").setLevel(logging.CRITICAL)
logging.getLogger("aiogrpc.utils").setLevel(logging.CRITICAL)

MISSION_ITEM_SIGNATURE = str(inspect.signature(MissionItem.__init__))
_MISSION_ITEM_SIGNATURE_LOGGED = False

# Типы миссий, которые исполняются НАТИВНЫМИ командами (goto_location + do_orbit
# + hover), а не PX4-миссией. Причина: mission-loiter у мультикоптера в PX4 SIH
# вместо зависания плавно сажает дрон на землю. goto/do_orbit держат высоту.
# delivery / sector остаются на PX4-миссии (короткие/без длинных зависаний).
NATIVE_MISSION_TYPES = {"isr", "patrol"}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _is_shutdown_error(e: BaseException) -> bool:
    """Признаки того, что gRPC-стрим обвалился из-за остановки mavsdk_server,
    а не из-за реальной проблемы. Такие случаи логируем как info, не error."""
    s = str(e)
    return (
        "Socket closed" in s
        or "StatusCode.UNAVAILABLE" in s
        or "Cancelled" in s
        or isinstance(e, asyncio.CancelledError)
    )


def _log_stream_stop(name: str, what: str, e: BaseException) -> None:
    if _is_shutdown_error(e):
        log.info(f"[{name}] {what} stream stopped (shutdown)")
    else:
        log.error(f"[{name}] ❌ Ошибка телеметрии {what}: {e!r}")


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
        # NB: НЕ переопределяем MPC_XY_VEL_MAX / MPC_XY_CRUISE / MPC_Z_VEL_MAX_DN
        # / MPC_LAND_SPEED — дефолты PX4 откалиброваны под SIH-модель. Любая
        # «оптимизация» этих параметров приводит к потере высоты во время
        # transit'а после loiter-WP (диагностика 2026-06-05).
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
#  Нативный executor маршрута (ISR / Patrol)
# =====================================================
async def run_native_mission(
    sys: System,
    name: str,
    bus: MqttBus,
    state_ctx: dict,
    telem_state: dict,
    mission_id: str,
    waypoints: list,
) -> None:
    """Исполняет список waypoints нативными командами MAVSDK.

    - NAV: goto_location до точки, затем (если hold_s>0) HOVER на месте hold_s сек.
    - ORBIT: подлёт к центру, затем do_orbit(radius, vel) на hold_s сек (ISR-круг).
    - LAND: action.land() и ожидание касания земли.

    Высота берётся как ground_AMSL + relative_alt (ground = alt_abs - alt_rel).
    Прогресс публикуется в mission/{id}/progress; завершение (COMPLETED) ловит
    publish_fleet_active по landed + current>=total.
    """
    total = len(waypoints)
    telem_state["mission_total"] = total
    telem_state["mission_current"] = 0

    def _ground_amsl() -> float:
        return float(telem_state["alt_abs"]) - float(telem_state["alt_rel"])

    def _publish_progress(cur: int) -> None:
        bus.publish(f"mission/{mission_id}/progress", {
            "mission_id": mission_id,
            "vehicle_id": name,
            "current": int(cur),
            "total": int(total),
            "ts": time.time(),
        }, qos=0)

    async def _hold(dur_s: float, cur: int) -> None:
        """Удерживаем текущую позицию dur_s сек (hover/orbit держатся сами)."""
        t0 = time.time()
        while time.time() - t0 < dur_s:
            _publish_progress(cur)
            await asyncio.sleep(1.0)

    async def _wait_reached(lat: float, lon: float, rel_alt: float, accept: float, cur: int) -> bool:
        d0 = _haversine_m(telem_state["lat"], telem_state["lon"], lat, lon)
        dz0 = abs(float(telem_state["alt_rel"]) - rel_alt)
        timeout = min(180.0, max(25.0, d0 / 3.0 + dz0 + 25.0))
        acc = max(accept, 4.0)
        t0 = time.time()
        d = d0
        dz = dz0
        while time.time() - t0 < timeout:
            d = _haversine_m(telem_state["lat"], telem_state["lon"], lat, lon)
            dz = abs(float(telem_state["alt_rel"]) - rel_alt)
            if d <= acc and dz <= 3.0:
                return True
            _publish_progress(cur)
            await asyncio.sleep(0.5)
        log.warning(f"[{name}] native: reach timeout (d_left≈{d:.0f}m dz≈{dz:.1f}m)")
        return False

    try:
        from mavsdk.action import OrbitYawBehavior
        yaw_behavior = OrbitYawBehavior.HOLD_FRONT_TO_CIRCLE_CENTER
    except Exception:
        yaw_behavior = 0

    log.info(f"[{name}] [MISSION] native exec begin mission_id={mission_id} wps={total}")
    try:
        for idx, wp in enumerate(waypoints):
            pos = wp.get("pos", wp)
            lat = float(pos["lat"])
            lon = float(pos["lon"])
            rel_alt = float(pos["alt"])
            kind = str(wp.get("kind", "NAV")).upper()
            hold_s = float(wp.get("hold_s") or 0.0)
            orbit_r = float(wp.get("orbit_radius_m") or 0.0)
            speed = float(wp.get("speed_m_s") or 5.0)
            accept = float(wp.get("acceptance_radius_m") or 5.0)
            _publish_progress(idx)

            if kind == "LAND":
                log.info(f"[{name}] native[{idx + 1}/{total}] LAND @ ({lat:.6f},{lon:.6f})")
                await sys.action.land()
                t0 = time.time()
                while time.time() - t0 < 90.0:
                    if not bool(telem_state["in_air"]) and float(telem_state["alt_rel"]) < 1.0:
                        break
                    await asyncio.sleep(0.5)
                log.info(f"[{name}] native LAND touchdown alt={float(telem_state['alt_rel']):.1f}m")
                break

            abs_alt = _ground_amsl() + rel_alt

            if kind == "ORBIT" and orbit_r > 0:
                orbit_vel = max(2.0, min(speed, 8.0))
                log.info(
                    f"[{name}] native[{idx + 1}/{total}] ORBIT r={orbit_r:.0f}m "
                    f"v={orbit_vel:.1f}m/s dur={hold_s:.0f}s @ {rel_alt:.0f}m"
                )
                # Подлетаем к центру цели на нужной высоте, затем кружим.
                await sys.action.goto_location(lat, lon, abs_alt, float("nan"))
                await _wait_reached(lat, lon, rel_alt, max(orbit_r, accept), idx)
                try:
                    await sys.action.do_orbit(orbit_r, orbit_vel, yaw_behavior, lat, lon, abs_alt)
                except Exception as e:
                    log.error(f"[{name}] do_orbit fail: {e!r} — fallback hover")
                await _hold(max(hold_s, 5.0), idx)
            else:
                # NAV (а также ORBIT с radius=0 = просто зависание на точке).
                log.info(
                    f"[{name}] native[{idx + 1}/{total}] GOTO ({lat:.6f},{lon:.6f}) "
                    f"alt={rel_alt:.0f}m hold={hold_s:.0f}s"
                )
                await sys.action.goto_location(lat, lon, abs_alt, float("nan"))
                await _wait_reached(lat, lon, rel_alt, accept, idx)
                if hold_s > 0:
                    log.info(f"[{name}] native[{idx + 1}/{total}] HOVER {hold_s:.0f}s")
                    await _hold(hold_s, idx)

            telem_state["mission_current"] = idx + 1
            _publish_progress(idx + 1)

        telem_state["mission_current"] = total
        _publish_progress(total)
        log.info(f"[{name}] [MISSION] native exec done mission_id={mission_id}")
    except asyncio.CancelledError:
        log.info(f"[{name}] native mission cancelled mission_id={mission_id}")
        raise
    except Exception as e:
        log.error(f"[{name}] ❌ native mission error: {e!r}")
        _set_state(state_ctx, name, "error", reason="native exec failed")
        try:
            _publish_mission_status(bus, mission_id, name, "ABORTED", error=repr(e))
        except Exception:
            pass


# =====================================================
#  Обработка MQTT команд
# =====================================================
async def handle_command(msg, sys: System, name: str, bus: MqttBus, state_ctx: dict, telem_state: dict | None = None):
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

            # takeoff_alt_m в payload передан явно оркестратором (из UI).
            # Раньше брался из alt первого WP — это путало семантику:
            # «крейсерская высота первого NAV» != «целевая высота автовзлёта».
            # Теперь это разные параметры, и UI задаёт их раздельно.
            takeoff_alt_payload = payload.get("takeoff_alt_m")
            if takeoff_alt_payload is not None:
                state_ctx["takeoff_alt_m"] = float(takeoff_alt_payload)
            else:
                # Legacy fallback (на случай старых клиентов).
                first_wp = waypoints[0]
                first_pos = first_wp.get("pos", first_wp)
                state_ctx["takeoff_alt_m"] = float(first_pos["alt"])
            first_wp = waypoints[0]
            state_ctx["first_is_takeoff"] = str(first_wp.get("kind", "NAV")).upper() == "TAKEOFF"
            state_ctx["mission_type"] = str(payload.get("mission_type") or "delivery").lower()
            # Профиль взлёта: vertical (набор прямо до cruise, потом полёт) или
            # inclined (короткий вертикальный отрыв до takeoff_alt, дальше набор
            # до cruise по диагонали к первой точке). cruise_alt берём из payload,
            # иначе из alt первого NAV-вейпоинта.
            state_ctx["takeoff_profile"] = str(payload.get("takeoff_profile") or "vertical").lower()
            cruise_alt_payload = payload.get("cruise_alt_m")
            if cruise_alt_payload is not None:
                state_ctx["cruise_alt_m"] = float(cruise_alt_payload)
            else:
                fp = first_wp.get("pos", first_wp)
                state_ctx["cruise_alt_m"] = float(fp.get("alt", state_ctx["takeoff_alt_m"]))
            log.info(
                "[%s] mission.upload takeoff_alt=%.1fm profile=%s cruise=%.1fm type=%s wps=%d",
                name, state_ctx["takeoff_alt_m"], state_ctx["takeoff_profile"],
                state_ctx["cruise_alt_m"], state_ctx["mission_type"], len(waypoints),
            )

            # Сохраняем raw waypoints для ВСЕХ типов: нужны и нативному executor'у,
            # и единому детерминированному взлёту в mission.start (для inclined —
            # узнать горизонтальную позицию первой точки маршрута).
            state_ctx["waypoints"] = waypoints

            # ISR / Patrol исполняем НАТИВНО (goto_location + do_orbit + hover),
            # а не PX4-миссией: mission-loiter у мультикоптера в SIH вместо
            # зависания плавно сажает дрон на землю (проверено 2026-06-05,
            # 40м→0 за ~40с). goto/do_orbit держат высоту чисто. Поэтому здесь
            # PX4-миссию НЕ заливаем.
            if state_ctx["mission_type"] in NATIVE_MISSION_TYPES:
                log.info(
                    "[%s] [MISSION] native exec mode (type=%s) — PX4 mission upload skipped",
                    name, state_ctx["mission_type"],
                )
                _publish_mission_status(bus, mission_id, name, "UPLOADED")
                log.info(f"[{name}] [MISSION] upload result=UPLOADED mission_id={mission_id}")
                _set_state(state_ctx, name, "mission_uploaded")
                return

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
                # Точная фиксация на hold-WP: если задан loiter_time_s>0 — это
                # «зависание» (фаза hover/pickup/loiter), и пролетать сквозь
                # точку нельзя. Для обычных NAV — fly-through (плавный transit).
                # LAND всегда fly-to.
                is_fly_through = (kind == "NAV") and (hold_s <= 0.0)
                # Speed: берём из waypoint'а если задан, иначе дефолт 5
                speed_m_s = float(wp.get("speed_m_s") or 5.0)
                # Clamp в адекватные пределы (PX4 ругается на 0 или слишком большие)
                speed_m_s = max(1.0, min(30.0, speed_m_s))
                # acceptance_radius: «считать достигнутым» — берём из WP если задан.
                acceptance_radius_m = float(wp.get("acceptance_radius_m") or 5.0)
                acceptance_radius_m = max(1.0, min(50.0, acceptance_radius_m))

                mission_items.append(
                    MissionItem(
                        latitude_deg=lat,
                        longitude_deg=lon,
                        relative_altitude_m=alt,
                        speed_m_s=speed_m_s,
                        is_fly_through=is_fly_through,
                        gimbal_pitch_deg=0.0,
                        gimbal_yaw_deg=0.0,
                        camera_action=MissionItem.CameraAction.NONE,
                        loiter_time_s=hold_s,
                        camera_photo_interval_s=0.0,
                        acceptance_radius_m=acceptance_radius_m,
                        yaw_deg=0.0,
                        camera_photo_distance_m=0.0,
                        vehicle_action=vehicle_action,
                    )
                )
                uploaded_idx += 1
                log.info(
                    "[%s] mission.upload wp#%d -> px4_idx=%d lat=%.7f lon=%.7f alt=%.2f "
                    "hold_s=%.2f speed=%.1f accept_r=%.1f action=%s fly_through=%s",
                    name, idx, uploaded_idx - 1, lat, lon, alt, hold_s, speed_m_s,
                    acceptance_radius_m, vehicle_action, is_fly_through,
                )

            plan = MissionPlan(mission_items)
            # Чистим предыдущую миссию перед заливкой: иначе PX4 может сохранить
            # current-index от прошлой миссии той же длины и стартовать НЕ с первой
            # точки (наблюдалось: старт сразу с LAND-итема → дрон садится у базы).
            try:
                await sys.mission.clear_mission()
            except Exception as clr_err:
                log.warning(f"[{name}] clear_mission warn: {clr_err!r}")
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

            if telem_state is None:
                raise RuntimeError("mission.start requires telem_state")

            mission_type = str(state_ctx.get("mission_type") or "delivery").lower()
            takeoff_alt_m = float(state_ctx.get("takeoff_alt_m", 15.0))
            cruise_alt_m = float(state_ctx.get("cruise_alt_m", takeoff_alt_m))
            takeoff_profile = str(state_ctx.get("takeoff_profile", "vertical")).lower()
            waypoints = list(state_ctx.get("waypoints") or [])

            # === ЕДИНЫЙ ДЕТЕРМИНИРОВАННЫЙ ВЗЛЁТ (для ВСЕХ типов миссий) ===
            # Набор высоты делаем САМИ нативным goto, а не полагаемся на PX4
            # auto-takeoff + AUTO.MISSION (мультикоптер там всегда climb-first и
            # игнорирует профиль → раньше vertical/inclined для delivery выглядели
            # одинаково «как захотят»). Теперь профиль работает железно для всех.
            #  - vertical: короткий отрыв → набор ПРЯМО ВВЕРХ до cruise над точкой
            #    старта → дальше горизонтальный полёт.
            #  - inclined: короткий отрыв → ДИАГОНАЛЬНЫЙ набор до cruise в сторону
            #    первой точки маршрута.
            lift_alt = max(3.0, min(takeoff_alt_m, cruise_alt_m))
            try:
                await sys.action.set_takeoff_altitude(lift_alt)
            except Exception as set_alt_err:
                log.warning(f"[{name}] set_takeoff_altitude({lift_alt}) warn: {set_alt_err!r}")
            log.info(f"[{name}] 🛫 liftoff → {lift_alt:.1f}m (profile={takeoff_profile})")
            await sys.action.takeoff()

            # ждём отрыва от земли
            t0 = time.time()
            while time.time() - t0 < 30.0:
                if float(telem_state["alt_rel"]) >= max(3.0, lift_alt - 1.5):
                    break
                await asyncio.sleep(0.3)
            else:
                log.error(f"[{name}] ❌ timeout: дрон не оторвался за 30с")
                raise RuntimeError("takeoff timeout")
            log.info(f"[{name}] ✈️ airborne alt={float(telem_state['alt_rel']):.2f}m")

            # цель набора высоты по профилю
            ground_amsl = float(telem_state["alt_abs"]) - float(telem_state["alt_rel"])
            cruise_abs = ground_amsl + cruise_alt_m
            # первая «реальная» точка маршрута (не TAKEOFF/LAND) — для inclined
            first_nav = None
            for wp in waypoints:
                k = str(wp.get("kind", "NAV")).upper()
                if k not in ("TAKEOFF", "LAND"):
                    first_nav = wp.get("pos", wp)
                    break
            if takeoff_profile == "inclined" and first_nav is not None:
                climb_lat, climb_lon = float(first_nav["lat"]), float(first_nav["lon"])
                log.info(f"[{name}] ↗ INCLINED climb → first WP ({climb_lat:.6f},{climb_lon:.6f}) @ {cruise_alt_m:.0f}m")
            else:
                climb_lat, climb_lon = float(telem_state["lat"]), float(telem_state["lon"])
                log.info(f"[{name}] ↑ VERTICAL climb → {cruise_alt_m:.0f}m над точкой старта")
            try:
                await sys.action.goto_location(climb_lat, climb_lon, cruise_abs, float("nan"))
            except Exception as e:
                log.warning(f"[{name}] climb goto warn: {e!r}")
            # ждём набора крейсерской высоты
            t0 = time.time()
            climb_timeout = min(120.0, cruise_alt_m / 1.5 + 30.0)
            while time.time() - t0 < climb_timeout:
                if float(telem_state["alt_rel"]) >= cruise_alt_m - 3.0:
                    break
                await asyncio.sleep(0.3)
            log.info(f"[{name}] reached climb alt={float(telem_state['alt_rel']):.1f}m (target {cruise_alt_m:.0f})")

            # === Запуск маршрута ===
            if mission_type in NATIVE_MISSION_TYPES:
                # Нативный executor: goto/do_orbit/hover/land. PX4-миссию не трогаем.
                _publish_mission_status(bus, mission_id, name, "STARTED")
                log.info(f"[{name}] [MISSION] start result=STARTED mission_id={mission_id} (native)")
                _set_state(state_ctx, name, "mission_running")
                task = asyncio.create_task(
                    run_native_mission(sys, name, bus, state_ctx, telem_state, mission_id, waypoints)
                )
                state_ctx["native_task"] = task
            else:
                # PX4-миссия: дрон уже на cruise (взлёт сделан выше) → AUTO.MISSION
                # летит горизонтально, без climb-first.
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

    # Общее состояние между корутинами (latest telemetry).
    # Определяем ДО подписки на команды — _on_command замыкается на telem_state,
    # а нативный executor (ISR/patrol) читает из него live-позицию дрона.
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

    # подписка на команды
    loop = asyncio.get_running_loop()

    def _on_command(message):
        asyncio.run_coroutine_threadsafe(handle_command(message, sys, name, bus, state_ctx, telem_state), loop)

    bus.subscribe(
        f"cmd/{name}/#",
        _on_command,
        qos=1,
    )
    log.info(f"[{name}] 🔔 Подписан на cmd/{name}/#")

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
        except BaseException as e:
            _log_stream_stop(name, "позиции", e)

    # --- режим полёта (MANUAL/ALTITUDE/OFFBOARD/MISSION/HOLD/RETURN/LAND...) ---
    async def log_flight_mode():
        try:
            last = None
            async for mode in sys.telemetry.flight_mode():
                if mode != last:
                    log.info(f"[{name}] [MODE] flight_mode={mode}")
                    last = mode
        except BaseException as e:
            _log_stream_stop(name, "flight_mode", e)

    # --- armed/disarmed ---
    async def log_armed():
        try:
            last = None
            async for armed in sys.telemetry.armed():
                telem_state["armed"] = armed
                if armed != last:
                    log.info(f"[{name}] [ARM] armed={armed}")
                    last = armed
        except BaseException as e:
            _log_stream_stop(name, "armed", e)

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
        except BaseException as e:
            _log_stream_stop(name, "mission_progress", e)

    # --- actuator outputs: реально ли PX4 крутит моторы + публикация в MQTT ---
    async def publish_actuators():
        try:
            last_publish = 0.0
            last_logged = 0.0
            async for ao in sys.telemetry.actuator_output_status():
                now = time.time()
                outputs = list(getattr(ao, "actuator", []) or [])
                first4 = [round(float(v), 1) for v in outputs[:4]]

                # rate-лимит публикации: 2 Hz
                if now - last_publish >= 0.5 and len(first4) >= 4:
                    bus.publish(f"telem/{name}/actuators", {
                        "motors": first4,                     # PWM us, обычно 1000..2000
                        "active": int(getattr(ao, "active", 0) or 0),
                        "ts": now,
                    }, qos=0)
                    last_publish = now

                # лог-трасса 0.5 Hz
                if now - last_logged >= 2.0:
                    log.info(f"[{name}] [ACT] motors[0:4]={first4} active={getattr(ao, 'active', '?')}")
                    last_logged = now
        except BaseException as e:
            _log_stream_stop(name, "actuator_output_status", e)

    # --- battery: voltage + remaining_percent → MQTT ---
    async def publish_battery():
        try:
            last_publish = 0.0
            async for b in sys.telemetry.battery():
                now = time.time()
                if now - last_publish < 1.0:
                    continue
                # remaining_percent в MAVSDK = 0..1 (доля). Преобразуем в проценты.
                pct = float(getattr(b, "remaining_percent", 0.0) or 0.0)
                pct = pct * 100.0 if pct <= 1.5 else pct  # на случай если уже в %
                bus.publish(f"telem/{name}/battery", {
                    "voltage_v": float(getattr(b, "voltage_v", 0.0) or 0.0),
                    "remaining_pct": round(pct, 1),
                    "ts": now,
                }, qos=0)
                last_publish = now
        except BaseException as e:
            _log_stream_stop(name, "battery", e)

    # --- attitude: pitch / roll / yaw в градусах ---
    async def publish_attitude():
        try:
            last_publish = 0.0
            async for a in sys.telemetry.attitude_euler():
                now = time.time()
                if now - last_publish < 0.4:  # ~2.5 Hz
                    continue
                bus.publish(f"telem/{name}/attitude", {
                    "pitch": round(float(a.pitch_deg), 2),
                    "roll": round(float(a.roll_deg), 2),
                    "yaw": round(float(a.yaw_deg), 2),
                    "ts": now,
                }, qos=0)
                last_publish = now
        except BaseException as e:
            _log_stream_stop(name, "attitude", e)

    # --- GPS info: num_satellites + fix_type ---
    async def publish_gps_info():
        try:
            last_publish = 0.0
            async for g in sys.telemetry.gps_info():
                now = time.time()
                if now - last_publish < 1.0:
                    continue
                # fix_type — enum, FIX_TYPE_3D и т.п.
                fix_str = str(getattr(g, "fix_type", "")).split(".")[-1] or "UNKNOWN"
                bus.publish(f"telem/{name}/gps", {
                    "satellites": int(getattr(g, "num_satellites", 0) or 0),
                    "fix_type": fix_str,
                    "ts": now,
                }, qos=0)
                last_publish = now
        except BaseException as e:
            _log_stream_stop(name, "gps_info", e)

    # --- velocity NED → ground speed (m/s) ---
    async def publish_velocity():
        try:
            last_publish = 0.0
            async for v in sys.telemetry.velocity_ned():
                now = time.time()
                if now - last_publish < 0.4:
                    continue
                gs = (float(v.north_m_s) ** 2 + float(v.east_m_s) ** 2) ** 0.5
                bus.publish(f"telem/{name}/velocity", {
                    "gs": round(gs, 2),
                    "vz": round(float(v.down_m_s), 2),  # down положительный = снижается
                    "ts": now,
                }, qos=0)
                last_publish = now
        except BaseException as e:
            _log_stream_stop(name, "velocity_ned", e)

    # --- in_air тракер (для статуса FLYING/IDLE) ---
    async def track_in_air():
        try:
            async for in_air in sys.telemetry.in_air():
                telem_state["in_air"] = in_air
        except BaseException as e:
            _log_stream_stop(name, "in_air", e)

    # --- периодическая публикация fleet/active + закрытие миссии по mission_progress ---
    async def publish_fleet_active():
        last_status = None
        mission_completed_published = False  # чтобы не шлять COMPLETED повторно
        was_airborne = False  # дрон реально взлетал в текущей миссии
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

                # Критерий завершения миссии: дрон ВЗЛЕТАЛ в этой миссии и теперь
                # ПРИЗЕМЛИЛСЯ (не in_air, alt<1м). Раньше требовали строго
                # current==total, но PX4 иногда не тикает финальный waypoint —
                # и миссия зависала на 80% (4/5), хотя дрон уже сел. Теперь
                # завершаем по факту посадки после взлёта (mid-mission спуски в
                # delivery идут до 10м, не до земли — ложного срабатывания нет).
                tot = telem_state["mission_total"]
                mid = str(state_ctx.get("mission_id") or "unknown")
                mission_active = (
                    mid != "unknown"
                    and state_ctx.get("state") in {"mission_running", "landing", "rtl", "armed"}
                )
                if mission_active and telem_state["alt_rel"] > 3.0:
                    was_airborne = True
                landed = (not bool(telem_state["in_air"])) and telem_state["alt_rel"] < 1.0
                if (
                    mission_active
                    and not mission_completed_published
                    and tot > 0
                    and was_airborne
                    and landed
                ):
                    # Форсим 100% прогресс — UI/таймлайн показывает выполнено.
                    telem_state["mission_current"] = tot
                    bus.publish(f"mission/{mid}/progress", {
                        "mission_id": mid, "vehicle_id": name,
                        "current": int(tot), "total": int(tot), "ts": time.time(),
                    }, qos=0)
                    log.info(f"[{name}] 🏁 mission {mid} COMPLETED (airborne→landed, progress→{tot}/{tot})")
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
                    was_airborne = False

                # Готов к следующей миссии — сбрасываем флаги после перехода в idle.
                if mid == "unknown" and state_ctx.get("state") == "idle":
                    mission_completed_published = False
                    was_airborne = False

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
            publish_actuators(),
            publish_battery(),
            publish_attitude(),
            publish_gps_info(),
            publish_velocity(),
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
        float(sim_home["lat"]),
        float(sim_home["lon"]),
        float(sim_home.get("alt", 0.0)),
    )

    # Режим одного дрона: переменная DRONE_ID выбирает целевой борт из config.yaml.
    # Нужен для multi-drone, т.к. MAVSDK-Python в одном Python-процессе не умеет
    # корректно изолировать два System() (mavsdk_server'ы «слипают» target system).
    # sim_supervisor и run_system.py запускают по одному bridge-процессу на дрон.
    drone_id_env = os.environ.get("DRONE_ID")
    if drone_id_env is not None:
        drones = [d for d in drones if str(d["id"]) == str(drone_id_env)]
        if not drones:
            raise RuntimeError(f"DRONE_ID={drone_id_env} не найден в config.yaml")
        # Берём home-позицию из дрона (если задана), иначе из simulator.home.
        d0 = drones[0]
        if "home_lat" in d0:
            home_lat = float(d0["home_lat"])
        if "home_lon" in d0:
            home_lon = float(d0["home_lon"])

    # Asyncio-нативный обработчик SIGTERM/SIGINT — чтобы при остановке через
    # supervisor (sim_supervisor посылает SIGTERM) async for'ы корректно
    # отменялись, finally-блоки выполняли bus.stop(), а в логах не было
    # каскада "Socket closed" tracebacks из gRPC-стримов.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(_sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows / nested loop

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

    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, pending = await asyncio.wait(
            [stop_task, *tasks],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            log.info("📡 Получен сигнал остановки, отменяю стримы...")
        for t in pending:
            t.cancel()
        # Даём finally-блокам отработать (bus.stop, закрытие gRPC).
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        if not stop_task.done():
            stop_task.cancel()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
