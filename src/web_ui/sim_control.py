"""Supervisor для симуляционного backend'а: PX4 SITL + MAVSDK bridges + workers.

Запускается из лаунчер-роутов. Web UI крутится отдельно (в основном
FastAPI-процессе), его этот модуль НЕ трогает.

Singleton: одновременно можно держать только один sim-кластер.
"""
from __future__ import annotations
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Лимиты на N дронов: config.yaml заранее заполнен 10 готовыми дронами
# с разнесёнными home-позициями. Берём первые N штук (cfg["drones"][:N]).
MIN_DRONES = 1
MAX_DRONES = 10

# Убедимся что src/ в path — иначе simulator.* и drone_core.* не подхватятся
# когда модуль импортируется как web_ui.sim_control.
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from simulator.px4_launcher import start_px4_instances  # noqa: E402

# Те же паттерны что в run_system.py — для надёжного pkill при stop().
KILL_PATTERNS = (
    "bin/px4",
    "mavsdk_server",
    "simulator.mavsdk_bridge",
    "drone_core.workers.telemetry_ingest",
    "drone_core.workers.orchestrator",
)


class SimSupervisor:
    def __init__(self) -> None:
        self.children: List[subprocess.Popen] = []
        self.state: str = "stopped"  # stopped | starting | running | stopping | error
        self.error: Optional[str] = None
        self.num_drones: int = 0
        self.started_at: Optional[float] = None
        self._lock = asyncio.Lock()

    @property
    def cfg_path(self) -> Path:
        return _SRC / "simulator" / "config.yaml"

    def status(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "error": self.error,
            "started_at": self.started_at,
            "child_count": len(self.children),
            "num_drones": self.num_drones,
        }

    async def start(self, num_drones: Optional[int] = None) -> Dict[str, Any]:
        async with self._lock:
            if self.state in ("running", "starting"):
                return {"ok": False, "error": "already_running", **self.status()}
            self.state = "starting"
            self.error = None
            self.children = []
            try:
                cfg = yaml.safe_load(self.cfg_path.read_text())

                # Берём первые N дронов из config.yaml (там их 10 заготовлено).
                # Если N не передан — используем simulator.num_drones из конфига.
                all_drones = cfg.get("drones") or []
                if num_drones is None:
                    n = int(cfg.get("simulator", {}).get("num_drones", 2))
                else:
                    n = int(num_drones)
                n = max(MIN_DRONES, min(MAX_DRONES, n))
                if n > len(all_drones):
                    raise RuntimeError(
                        f"Запрошено {n} дронов, но в config.yaml только {len(all_drones)}. "
                        f"Добавь записи в drones[] или уменьши N."
                    )
                cfg["drones"] = all_drones[:n]
                self.num_drones = n

                # 1) PX4 SITL
                px4_procs = await start_px4_instances(cfg)
                self.children.extend(px4_procs)
                await asyncio.sleep(1.0)

                # 2) MAVSDK Bridge per drone — bridge сам читает config.yaml и
                # фильтрует по DRONE_ID. Используем legacy-путь без env-overrides.
                for d in cfg["drones"]:
                    did = str(d["id"])
                    self._spawn(
                        f"MAVSDK Bridge {did}",
                        ["python", "-m", "simulator.mavsdk_bridge"],
                        cwd=str(_SRC),
                        env={"DRONE_ID": did},
                    )
                    await asyncio.sleep(0.4)

                # 3) Workers
                self._spawn(
                    "Telemetry Ingest",
                    ["python", "-m", "drone_core.workers.telemetry_ingest"],
                    cwd=str(_SRC),
                )
                await asyncio.sleep(0.4)
                self._spawn(
                    "Orchestrator",
                    ["python", "-m", "drone_core.workers.orchestrator"],
                    cwd=str(_SRC),
                )

                self.state = "running"
                self.started_at = time.time()
                return {"ok": True, **self.status()}
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                self.state = "error"
                # Аккуратно прибиваем то что успели поднять.
                self._kill_all()
                return {"ok": False, **self.status()}

    async def stop(self) -> Dict[str, Any]:
        async with self._lock:
            if self.state == "stopped":
                return {"ok": True, **self.status()}
            self.state = "stopping"
            self._kill_all()
            self.state = "stopped"
            self.started_at = None
            self.num_drones = 0
            return {"ok": True, **self.status()}

    def _spawn(
        self,
        name: str,
        cmd: List[str],
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> subprocess.Popen:
        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        print(f"[sim] ▶️ {name}: {' '.join(cmd)} (cwd={cwd})")
        p = subprocess.Popen(
            cmd,
            cwd=cwd,
            stderr=subprocess.STDOUT,
            text=True,
            env=run_env,
            start_new_session=True,
        )
        self.children.append(p)
        return p

    def _kill_all(self) -> None:
        """Двухфазная остановка: SIGTERM tracked → grace 2.5с → pkill сирот.

        Зачем graceful: при SIGKILL bridge'ам асинхронные gRPC-стримы валятся
        с "Socket closed" → каскад ERROR-tracebacks в логах. SIGTERM даёт
        bridge'у штатно отменить async for, выполнить bus.stop() в finally —
        логи остаются чистыми, MQTT-клиенты корректно дисконнектятся.

        ВАЖНО: используем send_signal на конкретный pid, а не killpg —
        не все наши дети стартовали с start_new_session=True (например,
        PX4 instances в px4_launcher.py). killpg на pgid 0 убил бы и нас.
        """
        # Phase 1: SIGTERM tracked children и их детям — даём 2.5с на graceful.
        for p in self.children:
            try:
                if p.poll() is None:
                    p.send_signal(signal.SIGTERM)
            except Exception:
                pass

        deadline = time.time() + 2.5
        for p in self.children:
            remaining = max(0.05, deadline - time.time())
            try:
                p.wait(timeout=remaining)
            except Exception:
                pass

        # Phase 2: pkill стрегглеров и сирот по cmdline-паттернам.
        # mavsdk_server — внук (форкнут библиотекой mavsdk внутри bridge'а),
        # SIGTERM bridge'у его не зацепит → нужен явный pkill.
        # SIGKILL здесь ОК: после grace всё что не успело — уже подвисло.
        for patt in KILL_PATTERNS:
            try:
                subprocess.run(
                    ["pkill", "-9", "-f", patt],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=3,
                )
            except Exception:
                pass

        self.children = []


sim_supervisor = SimSupervisor()