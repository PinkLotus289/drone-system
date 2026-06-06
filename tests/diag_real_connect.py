#!/usr/bin/env python3
"""E2E проверка «подключения реального дрона»: PX4 SITL выступает заглушкой
реального борта по UDP. Профиль → connect → мост (REAL, без SITL-параметров) →
дрон во флоте + телеметрия → ручная команда → disconnect.

Примечание: SITL без sim-параметров НЕ заармится (нет RC) — это ожидаемо;
проверяем ВЕСЬ путь подключения и доставки команды (реальный борт армится сам).
"""
from __future__ import annotations
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402
from diagnose_real_flight import start_px4, kill_all_leftovers, CFG, G, R, Y, B, NC  # noqa: E402
from drone_core.profiles import DroneProfile, profile_store  # noqa: E402

PORT = 8030
BASE = f"http://127.0.0.1:{PORT}"
SRC = Path(__file__).resolve().parents[1] / "src"
WEB_LOG = "/tmp/web_real.log"


def sh(*args) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def curl(method, path, *, cookie=None, data=None, save_cookie=None, want_code=False):
    cmd = ["curl", "-s", "-X", method, BASE + path]
    if want_code:
        cmd += ["-o", "/dev/null", "-w", "%{http_code}"]
    if cookie:
        cmd += ["-b", cookie]
    if save_cookie:
        cmd += ["-c", save_cookie]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    return sh(*cmd)


def main():
    kill_all_leftovers()
    sh("pkill", "-9", "-f", "uvicorn web_ui")
    time.sleep(1.0)

    # 1) PX4 SITL как «реальный» борт (UDP :14540)
    cfg = yaml.safe_load(CFG.read_text())
    px4 = start_px4(cfg["drones"][0])
    time.sleep(2.0)

    # 2) Профиль реального борта: udp://:14540 (как будто борт за UDP-мостом)
    prof = DroneProfile(name="testbird", display_name="Test Bird",
                        connection_type="udp", net_host="", net_port=14540,
                        home_lat=43.0747, home_lon=-89.3842)
    profile_store.save(prof)
    print(f"{G}[real]{NC} profile saved: testbird → {prof.connection_url()}")

    # 3) Веб-сервер (с auth+control+real)
    web = subprocess.Popen(
        ["python", "-m", "uvicorn", "web_ui.main:app", "--port", str(PORT), "--log-level", "info"],
        cwd=str(SRC), stdout=open(WEB_LOG, "w"), stderr=subprocess.STDOUT, start_new_session=True,
    )
    for _ in range(40):
        if curl("GET", "/login", want_code=True) == "200":
            break
        time.sleep(0.5)
    print(f"{G}[real]{NC} web up on {PORT}")

    cj = "/tmp/real_cj.txt"
    # 4) Login + control
    print("login:", curl("POST", "/api/login", data={"code": "skybite", "callsign": "FIELD"}, save_cookie=cj))
    print("acquire control:", curl("POST", "/api/control/acquire", cookie=cj, data={}))

    # 5) Connect профиль → мост к «борту»
    print("connect:", curl("POST", "/api/real/connect/testbird", cookie=cj, data={}))

    # 6) Ждём появления борта во флоте с телеметрией
    veh_seen = None
    for _ in range(40):
        fleet = json.loads(curl("GET", "/api/fleet", cookie=cj) or '{"fleet":[]}')
        for d in fleet.get("fleet", []):
            if d.get("id") == "veh_testbird" and d.get("lat"):
                veh_seen = d
                break
        if veh_seen:
            break
        time.sleep(1.0)
    if veh_seen:
        print(f"{G}[real] ✓ борт во флоте:{NC} id={veh_seen['id']} "
              f"lat={veh_seen.get('lat'):.5f} lon={veh_seen.get('lon'):.5f} status={veh_seen.get('status')}")
    else:
        print(f"{R}[real] ✗ борт не появился во флоте{NC}")

    # 7) Ручная команда (arm) — проверяем доставку до моста
    print("manual arm:", curl("POST", "/api/drone/testbird/command", cookie=cj, data={"action": "arm"}))
    time.sleep(2.0)

    # 8) Connections + disconnect
    print("connections:", curl("GET", "/api/real/connections", cookie=cj))
    print("disconnect:", curl("POST", "/api/real/disconnect/testbird", cookie=cj, data={}))
    time.sleep(1.5)

    # 9) Анализ лога моста (он пишет в stdout веб-сервера)
    log = Path(WEB_LOG).read_text(errors="ignore")
    checks = {
        "REAL bridge launched": "REAL bridge:" in log or "REAL drone bridge" in log,
        "MAVSDK connected": "MAVSDK connected" in log,
        "fleet/active announced": "Объявился во fleet/active" in log,
        "manual arm delivered": "cmd=arm" in log,
        "NO sitl params (real)": "SITL-параметры НЕ применяются" in log,
    }
    print(f"\n{B}=== bridge log checks ==={NC}")
    for k, v in checks.items():
        print(f"  {G+'✓'+NC if v else R+'✗'+NC} {k}")

    ok = veh_seen is not None and checks["MAVSDK connected"] and checks["manual arm delivered"] and checks["NO sitl params (real)"]
    print(f"\n  {'✅ REAL-CONNECT PATH OK' if ok else '❌ CHECK FAILED'}")

    # cleanup
    profile_store.delete("testbird")
    try:
        web.send_signal(2); web.wait(timeout=4)
    except Exception:
        web.kill()
    kill_all_leftovers()
    sh("pkill", "-9", "-f", "uvicorn web_ui")


if __name__ == "__main__":
    try:
        main()
    finally:
        kill_all_leftovers()
        sh("pkill", "-9", "-f", "uvicorn web_ui")
