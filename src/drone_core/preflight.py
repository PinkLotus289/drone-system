"""Общая оценка pre-flight — единый источник правды для гейта арминга.

Используется двумя сторонами:
  - `simulator.mavsdk_bridge` — живой снимок здоровья борта ПЕРЕД армингом
    (жёсткий гейт: не даёт заармиться, если что-то красное);
  - `web_ui.main` (`/api/real/preflight/{name}`) — тот же расчёт по кэшу
    телеметрии для чек-листа оператора в UI.

Держим логику и пороги в ОДНОМ месте, чтобы UI и гейт никогда не расходились.
Пороги переопределяются переменными окружения — крутятся без правки кода.

Формы входных снимков (оба вызывающих строят именно их):
  health  = {gyro_ok, accel_ok, mag_ok, local_pos_ok, global_pos_ok,
             home_ok, armable, firmware?}   — булевы флаги MAVSDK telemetry.health()
  gps     = {satellites: int, fix_type: str}
  battery = {pct: float(0..100), voltage_v: float}
"""
from __future__ import annotations

import os
import math
from typing import Any, Dict, List, Optional


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return default


# --- настраиваемые пороги (env override) ---
MIN_BATTERY_PCT: float = _f("PREFLIGHT_MIN_BATTERY_PCT", 30.0)   # %
MIN_SATELLITES: int = int(_f("PREFLIGHT_MIN_SATS", 6))
REQUIRE_HOME: bool = os.environ.get("PREFLIGHT_REQUIRE_HOME", "1") != "0"
REQUIRE_GLOBAL_POS: bool = os.environ.get("PREFLIGHT_REQUIRE_GLOBAL_POS", "1") != "0"

# --- test-hop (проверочная мини-миссия) ---
HOP_ALT_M: float = _f("PREFLIGHT_HOP_ALT_M", 0.5)       # целевая высота взлёта
HOP_MIN_ALT_M: float = _f("PREFLIGHT_HOP_MIN_ALT_M", 0.3)   # клэмп снизу
HOP_MAX_ALT_M: float = _f("PREFLIGHT_HOP_MAX_ALT_M", 1.5)   # сторож: выше → аварийная посадка
MOTOR_TEST_SEC: float = _f("PREFLIGHT_MOTOR_TEST_SEC", 2.0)  # длительность прокрутки моторов


def clamp_hop_alt(alt: float) -> float:
    """Ограничить высоту test-hop безопасным диапазоном [HOP_MIN, HOP_MAX]."""
    try:
        return max(HOP_MIN_ALT_M, min(HOP_MAX_ALT_M, float(alt)))
    except (TypeError, ValueError):
        return HOP_ALT_M


def _fix_ok(fix: Optional[str]) -> bool:
    """Годный фикс: 3D / DGPS (дифференциальный) / RTK — в любом написании."""
    f = str(fix or "").upper()
    return "3D" in f or "DGPS" in f or "RTK" in f


def evaluate(
    health: Optional[Dict[str, Any]],
    gps: Optional[Dict[str, Any]],
    battery: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Вернуть {ok, verdict, checks[], reasons[]} по снимкам телеметрии.

    ok=False, если хоть один обязательный пункт красный (не «warn», не «info»).
    Отсутствие данных = fail-safe: пустой health → не армимся.
    """
    health = health or {}
    gps = gps or {}
    battery = battery or {}

    checks: List[Dict[str, Any]] = []
    reasons: List[str] = []

    def add(key: str, label: str, value: str, ok: bool, warn: bool = False) -> None:
        status = "ok" if ok else ("warn" if warn else "bad")
        checks.append({"key": key, "label": label, "value": value, "status": status})
        if not ok and not warn:
            reasons.append(f"{label}: {value}")

    # heartbeat подразумевается (телеметрия идёт) — показываем как ok.
    add("heartbeat", "HEARTBEAT", "OK", True)

    # прошивка — информационно, не блокирует.
    fw = health.get("firmware")
    if fw:
        checks.append({"key": "firmware", "label": "FIRMWARE", "value": str(fw), "status": "info"})

    # калибровки — обязательны.
    add("gyro", "CALIB GYRO", "OK" if health.get("gyro_ok") else "FAIL", bool(health.get("gyro_ok")))
    add("accel", "CALIB ACCEL", "OK" if health.get("accel_ok") else "FAIL", bool(health.get("accel_ok")))
    add("mag", "CALIB MAG", "OK" if health.get("mag_ok") else "FAIL", bool(health.get("mag_ok")))

    # GPS-фикс + спутники.
    sats = gps.get("satellites")
    sats_n = int(sats) if isinstance(sats, (int, float)) else 0
    fix = gps.get("fix_type")
    gps_ok = _fix_ok(fix) and sats_n >= MIN_SATELLITES
    add(
        "gps", "GPS FIX",
        f"{fix or 'no fix'} · {sats_n} sat",
        gps_ok,
        warn=False,
    )

    # позиции.
    if REQUIRE_GLOBAL_POS:
        add("global_pos", "GLOBAL POS", "OK" if health.get("global_pos_ok") else "no",
            bool(health.get("global_pos_ok")))
    if REQUIRE_HOME:
        add("home", "HOME POS", "OK" if health.get("home_ok") else "no",
            bool(health.get("home_ok")))
    add("local_pos", "LOCAL POS / EKF", "OK" if health.get("local_pos_ok") else "no",
        bool(health.get("local_pos_ok")))

    # батарея.
    pct = battery.get("pct")
    v = battery.get("voltage_v")
    vtxt_v = f"{v:.2f} V" if isinstance(v, (int, float)) and v else None
    if isinstance(pct, (int, float)) and math.isfinite(pct) and 0 <= pct <= 100:
        vtxt = f"{vtxt_v} · {pct:.0f}%" if vtxt_v else f"{pct:.0f}%"
        add("battery", "BATTERY", vtxt, pct >= MIN_BATTERY_PCT)
    else:
        add("battery", "BATTERY", "charge unknown — configure battery monitor in QGC", False)
    voltage_ok = isinstance(v, (int, float)) and math.isfinite(v) and v > 0
    add("voltage", "BATTERY VOLTAGE", vtxt_v or "unknown", voltage_ok)

    # финальный вердикт самого PX4 — авторитетный.
    armable = health.get("armable")
    add("armable", "ARMABLE (PX4)", "YES" if armable is True else "NO / unknown", armable is True)

    ok = len(reasons) == 0
    return {
        "ok": ok,
        "verdict": "PASSED" if ok else "BLOCKED",
        "checks": checks,
        "reasons": reasons,
    }
