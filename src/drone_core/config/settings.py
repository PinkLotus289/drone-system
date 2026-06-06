from typing import Literal

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    ENV: str = "dev"
    MQTT_URL: str = "mqtt://127.0.0.1:1883"
    DB_URL: str = "postgresql+asyncpg://drone:dronepass@localhost:5432/drones"
    SLA_MAX_TOTAL_MIN: int = 20
    SLA_WAIT_PICKUP_SEC: int = 60
    SLA_WAIT_DROPOFF_SEC: int = 60
    REPO_IMPL: str = "mem"
    SYSTEM_MODE: Literal["test", "preflight", "full"] = "test"

    # --- Доступ / сессии (вход по общему код-паролю) ---
    ACCESS_CODE: str = "skybite"                 # общий код входа (в проде задать через env!)
    SESSION_SECRET: str = "dev-secret-change-me-please-32chars-minimum"  # ключ подписи cookie (env в проде!)
    SESSION_TTL_HOURS: int = 12
    COOKIE_SECURE: bool = False                  # True за HTTPS в проде
    CONTROL_TTL_SEC: float = 25.0                # таймаут аренды управления (арбитраж)

    class Config:
        env_file = ".env.dev"
        extra = "allow"
