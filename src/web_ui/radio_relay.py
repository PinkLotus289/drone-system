"""Radio-реле: мост между браузерным «связным» (Web Serial) и MAVSDK-мостом.

Зачем: при работе с удалённым сервером наземное USB-радио физически у оператора.
Браузер читает его через Web Serial и шлёт сырые MAVLink-байты на сервер по
WebSocket. Сервер не может отдать эти байты MAVSDK напрямую — MAVSDK умеет только
serial/udp/tcp. Поэтому на каждое радио-подключение мы поднимаем локальный
TCP-сервер на 127.0.0.1:<port>, к которому цепляется обычный mavsdk_bridge
(`tcp://127.0.0.1:<port>`), а реле гоняет байты в обе стороны:

    [drone] ~RF~ [USB SiK] --serial--> [browser Web Serial]
        --WebSocket--> [этот relay] <--localhost TCP--> [mavsdk_bridge] --MQTT--> UI

Направления:
  • браузер → реле → TCP-клиенты (MAVSDK): телеметрия/heartbeat от борта
  • TCP-клиент (MAVSDK) → реле → браузер: команды на борт

Всё крутится в event-loop FastAPI. Ключ реле — имя профиля борта.
"""
from __future__ import annotations
import asyncio
import time
from typing import Any, Dict, Optional, Set

# Диапазон локальных TCP-портов реле — выше gRPC mavsdk_server'ов
# (sim 50151+, тесты 50191+, real-bridge 50230+), чтобы не конфликтовать.
_PORT_BASE = 51000
_PORT_SPAN = 80


class RadioRelay:
    """Одно радио-подключение: локальный TCP-сервер ↔ один браузерный агент."""

    def __init__(self, name: str, tcp_port: int) -> None:
        self.name = name
        self.tcp_port = tcp_port
        self._server: Optional[asyncio.AbstractServer] = None
        self._tcp_writers: Set[asyncio.StreamWriter] = set()
        self._agent: Optional[Any] = None          # текущий браузерный WebSocket
        self._agent_since: Optional[float] = None
        self.bytes_to_drone = 0                     # браузер → MAVSDK
        self.bytes_from_drone = 0                   # MAVSDK → браузер
        self.last_rx: Optional[float] = None        # последний байт от борта

    # ---- lifecycle -------------------------------------------------
    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_tcp_client, host="127.0.0.1", port=self.tcp_port
        )
        print(f"[radio] relay '{self.name}' listening tcp://127.0.0.1:{self.tcp_port}")

    async def stop(self) -> None:
        # Закрываем агентский WS, TCP-клиентов и сам сервер.
        ag = self._agent
        self._agent = None
        if ag is not None:
            try:
                await ag.close()
            except Exception:
                pass
        for w in list(self._tcp_writers):
            try:
                w.close()
            except Exception:
                pass
        self._tcp_writers.clear()
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        print(f"[radio] relay '{self.name}' stopped")

    # ---- TCP side (MAVSDK bridge) ----------------------------------
    async def _on_tcp_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self._tcp_writers.add(writer)
        print(f"[radio] '{self.name}' MAVSDK client connected {peer} ({len(self._tcp_writers)} total)")
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                # команды MAVSDK → браузер → борт
                await self._send_to_agent(data)
        except (asyncio.CancelledError, ConnectionError):
            pass
        except Exception as e:
            print(f"[radio] '{self.name}' tcp read error: {e}")
        finally:
            self._tcp_writers.discard(writer)
            try:
                writer.close()
            except Exception:
                pass

    async def feed_from_agent(self, data: bytes) -> None:
        """Байты из браузера (от борта) → всем подключённым MAVSDK-клиентам."""
        if not data:
            return
        self.bytes_to_drone += len(data)
        self.last_rx = time.time()
        dead = []
        for w in list(self._tcp_writers):
            try:
                w.write(data)
                await w.drain()
            except Exception:
                dead.append(w)
        for w in dead:
            self._tcp_writers.discard(w)

    async def _send_to_agent(self, data: bytes) -> None:
        ag = self._agent
        if ag is None:
            return  # нет браузера — команды просто отбрасываем (борт не на связи)
        try:
            await ag.send_bytes(data)
            self.bytes_from_drone += len(data)
        except Exception:
            # WS отвалился — забываем агента, ждём переподключения.
            if self._agent is ag:
                self._agent = None
                self._agent_since = None

    # ---- agent (browser WS) side -----------------------------------
    def set_agent(self, ws: Any) -> Optional[Any]:
        """Назначить текущий браузерный WS. Возвращает прежний (если был) — его
        нужно закрыть вызывающему (одно радио = один активный агент)."""
        prev = self._agent if self._agent is not ws else None
        self._agent = ws
        self._agent_since = time.time()
        return prev

    def clear_agent(self, ws: Any) -> None:
        if self._agent is ws:
            self._agent = None
            self._agent_since = None

    @property
    def agent_connected(self) -> bool:
        return self._agent is not None

    def status(self) -> Dict[str, Any]:
        return {
            "tcp_port": self.tcp_port,
            "agent_connected": self.agent_connected,
            "agent_since": self._agent_since,
            "tcp_clients": len(self._tcp_writers),
            "bytes_to_drone": self.bytes_to_drone,
            "bytes_from_drone": self.bytes_from_drone,
            "last_rx": self.last_rx,
        }


class RadioRelayManager:
    """Реестр радио-реле по имени профиля + аллокация локальных TCP-портов."""

    def __init__(self) -> None:
        self._relays: Dict[str, RadioRelay] = {}
        self._lock = asyncio.Lock()

    def get(self, name: str) -> Optional[RadioRelay]:
        return self._relays.get(name)

    async def ensure(self, name: str) -> RadioRelay:
        """Вернуть (создав при необходимости) запущенное реле для профиля."""
        async with self._lock:
            r = self._relays.get(name)
            if r is not None and r._server is not None:
                return r
            # Подбираем свободный порт, пробуя bind по факту.
            used = {x.tcp_port for x in self._relays.values()}
            last_err: Optional[Exception] = None
            for off in range(_PORT_SPAN):
                port = _PORT_BASE + off
                if port in used:
                    continue
                relay = RadioRelay(name, port)
                try:
                    await relay.start()
                except OSError as e:
                    last_err = e
                    continue
                self._relays[name] = relay
                return relay
            raise RuntimeError(f"no free relay port in {_PORT_BASE}..{_PORT_BASE+_PORT_SPAN}: {last_err}")

    async def attach_agent(self, name: str, ws: Any) -> RadioRelay:
        relay = await self.ensure(name)
        prev = relay.set_agent(ws)
        if prev is not None:
            try:
                await prev.close()
            except Exception:
                pass
        return relay

    async def teardown(self, name: str) -> None:
        async with self._lock:
            r = self._relays.pop(name, None)
        if r is not None:
            await r.stop()

    def status(self) -> Dict[str, Any]:
        return {name: r.status() for name, r in self._relays.items()}


# Синглтон на процесс web-сервера.
radio_manager = RadioRelayManager()
