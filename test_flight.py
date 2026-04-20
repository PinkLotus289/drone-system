#!/usr/bin/env python3
import asyncio
from mavsdk import System

async def run():
    drone = System()
    print("🔌 Connecting to PX4 on udp://:14550 ...")
    await drone.connect(system_address="udp://127.0.0.1:14550")

    print("⏳ Waiting for connection...")
    async for state in drone.core.connection_state():
        if state.is_connected:
            print("✅ Drone discovered!")
            break

    print("🔋 Waiting for drone to be ready...")
    async for health in drone.telemetry.health():
        if health.is_global_position_ok and health.is_home_position_ok:
            print("✅ Drone ready for takeoff!")
            break

    print("🚀 Arming...")
    await drone.action.arm()

    print("⬆️ Taking off...")
    await drone.action.takeoff()
    await asyncio.sleep(8)

    print("⬇️ Landing...")
    await drone.action.land()

    print("✅ Mission complete!")

if __name__ == "__main__":
    asyncio.run(run())
