"""Stop the MAVSDK backend if its Python control loop dies or stops ticking.

MAVSDK resends Offboard setpoints on its own. An orphan backend must not keep a
drone in Offboard forever. The independent process watches a pipe, not the web
server. No pulse for 3 seconds (or EOF) kills the backend, allowing the verified
PX4 Offboard/GCS-loss failsafe to act. Linux and macOS only.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time


def supervise(command, input_fd=0, timeout=3.0):
    backend = subprocess.Popen(command)
    deadline = time.monotonic() + timeout
    try:
        while backend.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("[MAVSDK GUARD] control-loop timeout; stopping backend", flush=True)
                break
            ready, _, _ = select.select([input_fd], [], [], min(remaining, 0.2))
            if ready:
                if not os.read(input_fd, 4096):
                    print("[MAVSDK GUARD] controller exited; stopping backend", flush=True)
                    break
                deadline = time.monotonic() + timeout
    finally:
        if backend.poll() is None:
            backend.kill()
        backend.wait()


if __name__ == "__main__":
    supervise(sys.argv[1:])
