# scratch_repo_test.py
import asyncio
from datetime import datetime
from drone_core.config.settings import Settings
from drone_core.infra.repositories import make_repos
from drone_core.domain.models import Vehicle, LLA, Mission
import sys, os
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))


async def main():
    Settings()  # загрузит .env.dev
    fleet, missions = make_repos()

    v = Vehicle(name="sim-1", home=LLA(lat=52.0, lon=21.0, alt=30))
    await fleet.add(v)
    print("fleet free:", [x.id for x in await fleet.list_free()])

    m = Mission(pickup=LLA(lat=52.1, lon=21.1, alt=30),
                dropoff=LLA(lat=52.2, lon=21.2, alt=30),
                created_at=datetime.utcnow())
    await missions.create(m)
    got = await missions.get(m.id)
    print("mission:", got.id, got.status)

asyncio.run(main())
