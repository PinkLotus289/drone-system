"""Супервизор реальных дронов: на каждый подключённый профиль — один
mavsdk_bridge-процесс в режиме REAL (без SITL-параметров), который цепляется к
борту по его connection_url и публикует телеметрию/принимает команды через MQTT.

В отличие от sim_supervisor (PX4 SITL ×N), здесь PX4 не запускается — борт
реальный, мы только поднимаем мост к нему.
"""
from __future__ import annotations
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# gRPC-порты mavsdk_server для реальных мостов — выше sim (50151+id) и тестов (50191+).
_GRPC_BASE = 50230


class RealSupervisor:
    def __init__(self) -> None:
        # name -> {proc, grpc_port, url, started_at}
        self._conns: Dict[str, Dict[str, Any]] = {}
        self._next_grpc = _GRPC_BASE

    def _alloc_grpc(self) -> int:
        used = {c["grpc_port"] for c in self._conns.values()}
        p = self._next_grpc
        while p in used:
            p += 1
        self._next_grpc = p + 1
        if self._next_grpc > _GRPC_BASE + 60:
            self._next_grpc = _GRPC_BASE
        return p

    def is_connected(self, name: str) -> bool:
        c = self._conns.get(name)
        return bool(c and c["proc"].poll() is None)

    def connect(self, profile, url_override: Optional[str] = None) -> Dict[str, Any]:
        """profile: DroneProfile. Поднимает мост к реальному борту.

        url_override — готовый MAVSDK system_address (например, локальный
        tcp://127.0.0.1:<port> от radio-реле). Если задан, профильный
        connection_url() не вызывается (для radio он и не определён)."""
        name = profile.name
        if self.is_connected(name):
            return {"ok": False, "error": "already_connected", "name": name}
        try:
            url = url_override or profile.connection_url()
        except Exception as e:
            return {"ok": False, "error": f"bad_connection: {e}", "name": name}

        grpc_port = self._alloc_grpc()
        env = os.environ.copy()
        env.update({
            "REAL_BRIDGE": "1",
            "REAL_DRONE_ID": name,
            "REAL_CONN_URL": url,
            "REAL_GRPC_PORT": str(grpc_port),
        })
        if profile.home_lat is not None:
            env["REAL_HOME_LAT"] = str(profile.home_lat)
        if profile.home_lon is not None:
            env["REAL_HOME_LON"] = str(profile.home_lon)

        print(f"[real] ▶️ connect {name}: {url} (gRPC :{grpc_port})")
        proc = subprocess.Popen(
            ["python", "-m", "simulator.mavsdk_bridge"],
            cwd=str(_SRC), env=env,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._conns[name] = {
            "proc": proc, "grpc_port": grpc_port, "url": url, "started_at": time.time(),
        }
        return {"ok": True, "name": name, "url": url, "grpc_port": grpc_port}

    def disconnect(self, name: str) -> Dict[str, Any]:
        c = self._conns.pop(name, None)
        if not c:
            return {"ok": True, "name": name, "note": "not_connected"}
        proc = c["proc"]
        try:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
        except Exception:
            pass
        # mavsdk_server — внук моста, добиваем по gRPC-порту в cmdline.
        try:
            subprocess.run(["pkill", "-9", "-f", f"mavsdk_server.*-p {c['grpc_port']}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=3)
        except Exception:
            pass
        print(f"[real] ⏹️ disconnect {name}")
        return {"ok": True, "name": name}

    def status(self) -> Dict[str, Any]:
        out = {}
        for name, c in list(self._conns.items()):
            alive = c["proc"].poll() is None
            out[name] = {"connected": alive, "url": c["url"], "started_at": c["started_at"]}
            if not alive:
                self._conns.pop(name, None)  # подчистить мёртвые
        return out


real_supervisor = RealSupervisor()
