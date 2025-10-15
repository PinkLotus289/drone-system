from __future__ import annotations
from drone_core.config.settings import Settings
from .base import VehicleRepo, MissionRepo

def make_repos() -> tuple[VehicleRepo, MissionRepo]:
    s = Settings()
    if s.REPO_IMPL.lower() == "pg":
        from .fleet_pg import FleetPg, VehicleRow
        from .missions_pg import MissionsPg, MissionRow, WaypointRow
        # создадим таблицы, если нужно
        from drone_core.infra.db.postgres import create_all
        import asyncio
        # safe create_all on import-time (only once)
        asyncio.get_event_loop().run_until_complete(create_all(models_module=None))
        return FleetPg(), MissionsPg()
    else:
        from .fleet_mem import FleetMem
        from .missions_mem import MissionsMem
        return FleetMem(), MissionsMem()
