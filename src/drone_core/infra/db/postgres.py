from __future__ import annotations
from contextlib import asynccontextmanager
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from drone_core.config.settings import Settings

_engine: AsyncEngine | None = None

def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = Settings()
        _engine = create_async_engine(settings.DB_URL, echo=False, future=True)
    return _engine

@asynccontextmanager
async def session():
    engine = get_engine()
    async with AsyncSession(engine) as s:
        yield s

async def create_all(models_module) -> None:
    """Вызови один раз при старте сервиса, чтобы создать таблицы."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
