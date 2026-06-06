"""Арбитраж управления: одновременно командовать может только один клиент.

Лиз-аренда по «scope» (например "system" для диспетчеризации, либо id борта для
ручного управления конкретным дроном). Аренда с TTL + heartbeat: если держатель
пропал (закрыл вкладку / упал линк) — аренда протухает и борт освобождается сам.
Хранение в памяти процесса (для горизонтального масштабирования позже — Redis).
"""
from __future__ import annotations
import threading
import time
from typing import Optional


class ControlAuthority:
    def __init__(self, ttl: float = 25.0) -> None:
        self.ttl = ttl
        self._leases: dict[str, dict] = {}   # scope -> {operator, sid, expires}
        self._lock = threading.Lock()

    def _live(self, lease: Optional[dict]) -> bool:
        return bool(lease and lease["expires"] > time.time())

    def get(self, scope: str) -> Optional[dict]:
        with self._lock:
            l = self._leases.get(scope)
            return dict(l) if self._live(l) else None

    def acquire(self, scope: str, operator: str, sid: str):
        """Берёт управление, если свободно/протухло/уже наше.
        Возвращает (ok, lease|holder)."""
        with self._lock:
            l = self._leases.get(scope)
            if self._live(l) and l["sid"] != sid:
                return False, dict(l)
            l = {"operator": operator, "sid": sid, "expires": time.time() + self.ttl}
            self._leases[scope] = l
            return True, dict(l)

    def heartbeat(self, scope: str, sid: str) -> bool:
        with self._lock:
            l = self._leases.get(scope)
            if self._live(l) and l["sid"] == sid:
                l["expires"] = time.time() + self.ttl
                return True
            return False

    def release(self, scope: str, sid: str) -> bool:
        with self._lock:
            l = self._leases.get(scope)
            if l and l["sid"] == sid:
                del self._leases[scope]
                return True
            return False

    def takeover(self, scope: str, operator: str, sid: str) -> Optional[dict]:
        """Принудительный перехват. Возвращает прежнего держателя (если был)."""
        with self._lock:
            prev = self._leases.get(scope)
            prev_live = dict(prev) if self._live(prev) else None
            self._leases[scope] = {"operator": operator, "sid": sid, "expires": time.time() + self.ttl}
            return prev_live

    def is_holder(self, scope: str, sid: str) -> bool:
        with self._lock:
            l = self._leases.get(scope)
            return bool(self._live(l) and l["sid"] == sid)

    def snapshot(self) -> dict:
        """Все живые аренды: {scope: {operator, sid, expires}}."""
        with self._lock:
            now = time.time()
            return {
                k: {"operator": v["operator"], "sid": v["sid"], "expires": v["expires"]}
                for k, v in self._leases.items()
                if v["expires"] > now
            }
