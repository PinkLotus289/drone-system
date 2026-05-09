from __future__ import annotations
import re
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator

ConnectionType = Literal["serial", "udp", "tcp"]

# Имя профиля = slug: латиница+цифры+_-, чтобы безопасно писать в имя файла.
NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,40}$")


class DroneProfile(BaseModel):
    name: str = Field(..., description="Уникальное имя профиля, slug")
    display_name: str = Field("", description="Человекочитаемое имя для UI")
    connection_type: ConnectionType = "serial"

    # serial-only
    serial_port: Optional[str] = None
    serial_baud: Optional[int] = 57600

    # udp/tcp
    net_host: Optional[str] = None
    net_port: Optional[int] = None

    # Описание борта (для UI и алертов).
    frame: str = "X500"
    battery_cells: int = 4
    battery_min_voltage: float = 14.0  # 3.5В/cell для 4S

    # Опциональный home — если None, берём из GPS дрона при подключении.
    home_lat: Optional[float] = None
    home_lon: Optional[float] = None
    home_alt: Optional[float] = None

    notes: str = ""

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError(
                "name должно содержать только латиницу, цифры, _ и -, длина 1..40"
            )
        return v

    def connection_url(self) -> str:
        """MAVSDK system_address для этого профиля."""
        if self.connection_type == "serial":
            if not self.serial_port:
                raise ValueError("serial_port не задан")
            return f"serial://{self.serial_port}:{self.serial_baud or 57600}"
        if self.connection_type == "udp":
            return f"udp://{self.net_host or ''}:{self.net_port}"
        if self.connection_type == "tcp":
            return f"tcp://{self.net_host}:{self.net_port}"
        raise ValueError(f"Неизвестный connection_type: {self.connection_type}")