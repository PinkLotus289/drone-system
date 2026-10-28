import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


@pytest.mark.parametrize("failure", ["controller_exit", "stalled_loop"])
def test_guard_kills_backend_on_eof_or_missing_pulses(tmp_path, failure):
    pidfile = tmp_path / "backend.pid"
    dummy = "import os,time; from pathlib import Path; Path(__import__('sys').argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    code = "from simulator.mavsdk_guard import supervise; import sys; supervise(sys.argv[1:], timeout=0.5)"
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    guard = subprocess.Popen(
        [sys.executable, "-c", code, sys.executable, "-c", dummy, str(pidfile)], stdin=subprocess.PIPE, env=env
    )
    try:
        for _ in range(50):
            if pidfile.exists():
                break
            time.sleep(0.02)
        backend_pid = int(pidfile.read_text())
        for _ in range(4):
            guard.stdin.write(b".")
            guard.stdin.flush()
            time.sleep(0.1)
            assert guard.poll() is None
        if failure == "controller_exit":
            guard.stdin.close()
        guard.wait(timeout=3)
        with pytest.raises(ProcessLookupError):
            os.kill(backend_pid, 0)
    finally:
        if guard.stdin and not guard.stdin.closed:
            guard.stdin.close()
        if guard.poll() is None:
            guard.wait(timeout=3)
