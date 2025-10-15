from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Awaitable, Protocol, Optional, Union, Dict, Any

JSON = Dict[str, Any]
Handler = Union[Callable[["Message"], None], Callable[["Message"], Awaitable[None]]]

@dataclass
class Message:
    topic: str
    payload: Any            # dict/str/bytes — mqtt_bus приводит к dict если JSON
    qos: int
    retain: bool
    ts: float               # time.time()

class EventBus(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def publish(self, topic: str, payload: Any, qos: int = 1, retain: bool = False) -> None: ...
    def subscribe(self, topic: str, handler: Handler, qos: int = 1) -> None: ...
    def unsubscribe(self, topic: str, handler: Optional[Handler] = None) -> None: ...
