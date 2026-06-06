#!/usr/bin/env python3
"""E2E ручного управления через UI-API: arm → takeoff → goto на SIM-дроне.
SIM-борт умеет армиться (применяются sim-параметры). Проверяем весь путь:
auth → control → POST /api/drone/{id}/command → bridge → реальный набор высоты.
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
from diagnose_real_flight import start_px4, start_python, kill_all_leftovers, CFG, G, R, B, NC  # noqa: E402

PORT = 8032
B_ = f"http://127.0.0.1:{PORT}"
SRC = Path(__file__).resolve().parents[1] / "src"


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout.strip()


def curl(method, path, *, cookie=None, data=None, save=None, code=False):
    c = ["curl", "-s", "-X", method, B_ + path]
    if code:
        c += ["-o", "/dev/null", "-w", "%{http_code}"]
    if cookie:
        c += ["-b", cookie]
    if save:
        c += ["-c", save]
    if data is not None:
        c += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    return sh(*c)


def alt_of(cookie, veh="veh_0"):
    try:
        fleet = json.loads(curl("GET", "/api/fleet", cookie=cookie) or '{"fleet":[]}')
        for d in fleet.get("fleet", []):
            if d.get("id") == veh:
                return float(d.get("alt") or 0)
    except Exception:
        pass
    return 0.0


def main():
    kill_all_leftovers(); sh("pkill", "-9", "-f", "uvicorn web_ui"); time.sleep(1.0)
    cfg = yaml.safe_load(CFG.read_text())
    start_px4(cfg["drones"][0]); time.sleep(2.0)
    start_python("bridge-0", "simulator.mavsdk_bridge", {"DRONE_ID": "0"}); time.sleep(5.0)
    web = subprocess.Popen(["python", "-m", "uvicorn", "web_ui.main:app", "--port", str(PORT), "--log-level", "warning"],
                           cwd=str(SRC), stdout=open("/tmp/web_manual.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(40):
        if curl("GET", "/login", code=True) == "200":
            break
        time.sleep(0.5)

    cj = "/tmp/man_cj.txt"
    curl("POST", "/api/login", data={"code": "skybite", "callsign": "PILOT"}, save=cj)
    print("acquire control:", curl("POST", "/api/control/acquire", cookie=cj, data={}))

    # heartbeat в фоне — иначе аренда управления протухнет за время теста
    import threading
    _stop = threading.Event()
    def _hb():
        while not _stop.is_set():
            curl("POST", "/api/control/heartbeat", cookie=cj, data={})
            _stop.wait(8)
    threading.Thread(target=_hb, daemon=True).start()

    # ждём появления дрона
    for _ in range(30):
        if alt_of(cj) is not None and any(d.get("id") == "veh_0" for d in json.loads(curl("GET", "/api/fleet", cookie=cj) or '{"fleet":[]}').get("fleet", [])):
            break
        time.sleep(1.0)
    print(f"{G}[manual]{NC} drone present, alt={alt_of(cj):.1f}")

    print("arm:", curl("POST", "/api/drone/0/command", cookie=cj, data={"action": "arm"})); time.sleep(2.0)
    print("takeoff:", curl("POST", "/api/drone/0/command", cookie=cj, data={"action": "takeoff"}))

    # ждём набора высоты
    amax = 0.0
    for t in range(40):
        a = alt_of(cj); amax = max(amax, a)
        if t % 4 == 0:
            print(f"  [t+{t*2}s] alt={a:.1f}m")
        if a > 4.0:
            break
        time.sleep(2.0)

    # goto — увести в сторону на cruise
    print("goto:", curl("POST", "/api/drone/0/command", cookie=cj, data={"action": "goto", "lat": 43.0760, "lon": -89.3820, "alt": 30}))
    for t in range(20):
        a = alt_of(cj); amax = max(amax, a)
        if t % 3 == 0:
            print(f"  [goto t+{t*2}s] alt={a:.1f}m")
        if a > 20:
            break
        time.sleep(2.0)

    print("land:", curl("POST", "/api/drone/0/command", cookie=cj, data={"action": "land"}))
    time.sleep(2.0)

    _stop.set()
    log = Path("/tmp/web_manual.log").read_text(errors="ignore")
    delivered = "[MANUAL]" in log
    print(f"\n{B}=== verdict ==={NC}")
    print(f"  max alt reached: {amax:.1f}m")
    print(f"  manual cmds logged by server ([MANUAL]): {delivered}")
    ok = amax > 20 and delivered
    print(f"  {'✅ MANUAL CONTROL OK (armed, took off, climbed via goto)' if ok else '❌ CHECK'}")

    try:
        web.send_signal(2); web.wait(timeout=4)
    except Exception:
        web.kill()
    kill_all_leftovers(); sh("pkill", "-9", "-f", "uvicorn web_ui")


if __name__ == "__main__":
    try:
        main()
    finally:
        kill_all_leftovers(); sh("pkill", "-9", "-f", "uvicorn web_ui")
