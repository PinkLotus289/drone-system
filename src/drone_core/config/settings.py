from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    ENV: str = "dev"
    MQTT_URL: str = "mqtt://127.0.0.1:1883"
    DB_URL: str = "postgresql+asyncpg://drone:dronepass@localhost:5432/drones"
    SLA_MAX_TOTAL_MIN: int = 20
    SLA_WAIT_PICKUP_SEC: int = 60
    SLA_WAIT_DROPOFF_SEC: int = 60
    REPO_IMPL: str = "mem"

    class Config:
        env_file = ".env.dev"
        extra = "allow"
