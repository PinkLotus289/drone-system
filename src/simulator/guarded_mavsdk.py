"""Lifetime of a real-airframe MAVSDK backend, including Python crash/hang."""

from __future__ import annotations

import asyncio
from importlib.resources import files
import os
import signal
import subprocess
import sys

from mavsdk import System


async def run_guarded(connection_url, port, run):
    binary = str(files("mavsdk.bin").joinpath("mavsdk_server"))
    guard = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "simulator.mavsdk_guard",
            binary,
            "-p",
            str(port),
            "--sysid",
            "245",
            "--compid",
            "190",
            connection_url,
        ],
        stdin=subprocess.PIPE,
        start_new_session=True,
    )
    os.set_blocking(guard.stdin.fileno(), False)

    async def pulse():
        while True:
            if guard.poll() is not None:
                raise RuntimeError("MAVSDK guard/backend stopped")
            os.write(guard.stdin.fileno(), b".")
            await asyncio.sleep(0.2)

    pulse_task = asyncio.create_task(pulse())
    flight_task = None
    try:
        system = System(mavsdk_server_address="127.0.0.1", port=port)
        await asyncio.wait_for(system.connect(), timeout=15)
        flight_task = asyncio.create_task(run(system))
        done, _ = await asyncio.wait([pulse_task, flight_task], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        if flight_task:
            flight_task.cancel()
            await asyncio.gather(flight_task, return_exceptions=True)
        pulse_task.cancel()
        await asyncio.gather(pulse_task, return_exceptions=True)
        guard.stdin.close()
        try:
            await asyncio.wait_for(asyncio.to_thread(guard.wait), timeout=4)
        except asyncio.TimeoutError:
            os.killpg(guard.pid, signal.SIGKILL)
            await asyncio.to_thread(guard.wait)
