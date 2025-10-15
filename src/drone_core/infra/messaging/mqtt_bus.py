from __future__ import annotations
import json
import threading
import time
import queue
import ssl
import logging
from typing import Any, Dict, Optional, Callable, Awaitable, Union, List
from urllib.parse import urlparse

import paho.mqtt.client as mqtt

from .bus import EventBus, Message, Handler

log = logging.getLogger("mqtt-bus")

def _is_jsonable(obj: Any) -> bool:
    try:
        json.dumps(obj)
        return True
    except Exception:
        return False

class MqttBus(EventBus):
    """
    Лёгкая обёртка над paho-mqtt:
    - автопереподключение
    - подписка/публикация с QoS
    - диспатч хендлеров; поддержка async и sync функций
    - payload автоматически парсится из JSON (если это JSON)
    """

    def __init__(
        self,
        broker_url: str,
        client_id: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        keepalive: int = 30,
        clean_session: bool = True,
    ) -> None:
        self._url = urlparse(broker_url)
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id or f"drone-core-{int(time.time()*1000)}",
            clean_session=clean_session,
        )
        if username:
            self._client.username_pw_set(username, password or "")
        if self._url.scheme in ("mqtts", "ssl", "tls"):
            self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self._keepalive = keepalive

        # runtime
        self._connected = threading.Event()
        self._stop_evt = threading.Event()
        self._handlers: Dict[str, List[Handler]] = {}  # topic -> [handlers]
        self._lock = threading.RLock()

        # async-петля для корутинных обработчиков
        import asyncio
        self._async_loop = asyncio.new_event_loop()
        self._async_thread = threading.Thread(
            target=self._async_loop.run_forever, name="mqtt-async-loop", daemon=True
        )

        # отдельный тред для paho loop_forever
        self._mqtt_thread = threading.Thread(target=self._loop, name="mqtt-loop", daemon=True)

        # bind callbacks
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # ---------- lifecycle ----------
    def start(self) -> None:
        host = self._url.hostname or "localhost"
        port = self._url.port or (8883 if self._url.scheme in ("mqtts", "ssl", "tls") else 1883)
        log.info(f"MQTT connecting to {host}:{port} ({self._url.scheme})")
        self._client.connect_async(host, port, keepalive=self._keepalive)
        self._async_thread.start()
        self._mqtt_thread.start()
        # дождаться коннекта (не обязательно, но удобно)
        self._connected.wait(timeout=5)

    def stop(self) -> None:
        try:
            self._stop_evt.set()
            self._client.disconnect()
            self._client.loop_stop()
        finally:
            try:
                import asyncio
                self._async_loop.call_soon_threadsafe(self._async_loop.stop)
            except Exception:
                pass

    # ---------- pub/sub API ----------
    def publish(self, topic: str, payload: Any, qos: int = 1, retain: bool = False) -> None:
        if isinstance(payload, (dict, list)) or _is_jsonable(payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        elif isinstance(payload, (bytes, bytearray)):
            body = payload
        else:
            # последний шанс — строковое представление
            body = str(payload).encode("utf-8")

        if not self._connected.is_set():
            log.warning("publish while disconnected; message will still be queued by paho")
        res = self._client.publish(topic, body, qos=qos, retain=retain)
        # paho возвращает MQTTMessageInfo, можно проверить rc
        if res.rc != mqtt.MQTT_ERR_SUCCESS:
            log.error(f"publish error rc={res.rc} topic={topic}")

    def subscribe(self, topic: str, handler: Handler, qos: int = 1) -> None:
        with self._lock:
            self._handlers.setdefault(topic, []).append(handler)
        if self._connected.is_set():
            self._client.subscribe(topic, qos=qos)
            log.info(f"subscribed: {topic} (qos={qos})")

    def unsubscribe(self, topic: str, handler: Optional[Handler] = None) -> None:
        with self._lock:
            if handler is None:
                self._handlers.pop(topic, None)
            else:
                lst = self._handlers.get(topic, [])
                if handler in lst:
                    lst.remove(handler)
                    if not lst:
                        self._handlers.pop(topic, None)
        if self._connected.is_set():
            self._client.unsubscribe(topic)
            log.info(f"unsubscribed: {topic}")

    # ---------- callbacks ----------
    def _on_connect(self, client: mqtt.Client, userdata, flags, reason_code, properties) -> None:
        if reason_code == mqtt.MQTT_ERR_SUCCESS or reason_code == 0:
            log.info("MQTT connected")
            self._connected.set()
            # переподписываем все текущие топики
            with self._lock:
                for topic in self._handlers.keys():
                    client.subscribe(topic, qos=1)
                    log.debug(f"re-subscribed: {topic}")
        else:
            log.error(f"MQTT connect failed rc={reason_code}")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self._connected.clear()
        if not self._stop_evt.is_set():
            log.warning(f"MQTT disconnected reason_code={reason_code}; paho will reconnect")

    def _on_message(self, client: mqtt.Client, userdata, msg: mqtt.MQTTMessage) -> None:
        # попытка распарсить JSON
        payload: Any
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            try:
                payload = msg.payload.decode("utf-8")
            except Exception:
                payload = bytes(msg.payload)

        m = Message(
            topic=msg.topic,
            payload=payload,
            qos=msg.qos,
            retain=msg.retain,
            ts=time.time(),
        )
        # диспатчим всем хендлерам, подписанным ровно на этот топик
        # Paho сам делает matching wildcard-ов на стороне брокера.
        with self._lock:
            handlers = list(self._handlers.get(msg.topic, []))
            # плюс возможны wildcard-подписки — paho вернёт msg.topic, а подписывались мы на шаблон
            # поэтому пробежимся и по тем, кто подписан на шаблон, если есть точное совпадение уже покрыто
            if msg.topic not in self._handlers:
                for pattern, hs in self._handlers.items():
                    # простая эвристика: если клиент подписывался на pattern (например telem/+/pose),
                    # брокер всё равно доставит, а тут мы просто также вызовем обработчики.
                    if "+/" in pattern or "/#":
                        handlers.extend(hs)

        for h in handlers:
            try:
                if _is_coroutine(h):
                    import asyncio
                    asyncio.run_coroutine_threadsafe(h(m), self._async_loop)
                else:
                    h(m)
            except Exception as e:
                log.exception(f"handler error for topic={msg.topic}: {e}")

    def _loop(self) -> None:
        """Отдельный поток для paho loop_forever с авто-retry."""
        while not self._stop_evt.is_set():
            try:
                self._client.loop_forever()
            except Exception as e:
                log.exception(f"mqtt loop error: {e}")
                time.sleep(1.0)

def _is_coroutine(func: Handler) -> bool:
    import inspect
    return inspect.iscoroutinefunction(func) or isinstance(func, Callable) and hasattr(func, "__call__") and inspect.iscoroutinefunction(func)  # type: ignore
