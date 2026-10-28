#!/usr/bin/env python3
"""Read-only development environment check. Does not start services or drones."""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    errors = 0
    print(f"Project: {ROOT}")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    if Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        print("ERROR: run with .venv/bin/python scripts/check_dev.py")
        errors += 1
    if sys.version_info[:2] != (3, 12):
        print("WARN: this project environment was verified on Python 3.12")

    for filename in ("requirements.txt", "requirements-dev.txt"):
        for line in (ROOT / filename).read_text().splitlines():
            match = re.fullmatch(r"([\w-]+)(?:\[[^]]+\])?==([^\s#]+)", line.strip())
            if not match:
                continue
            name, expected = match.groups()
            try:
                actual = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                actual = "MISSING"
            ok = actual == expected
            print(f"{'OK' if ok else 'ERROR'}: {name} {actual} (expected {expected})")
            errors += int(not ok)

    env = dict(os.environ)
    env["PATH"] = f"{ROOT / '.venv/bin'}:/opt/homebrew/bin:/opt/homebrew/sbin:{env.get('PATH', '')}"
    for name in ("git", "code", "mosquitto", "cmake", "ninja", "make", "clang"):
        found = shutil.which(name, path=env["PATH"])
        print(f"{'OK' if found else 'WARN'}: {name}: {found or 'not found'}")

    px4 = ROOT / "PX4-Autopilot/build/px4_sitl_default/bin/px4"
    print(f"{'OK' if px4.is_file() else 'WARN'}: PX4 SITL binary {'exists' if px4.is_file() else 'missing'}")
    for port, label in ((1883, "MQTT"), (8000, "Web UI"), (5432, "Postgres (optional for REPO_IMPL=mem)")):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                print(f"INFO: {label}: port {port} accepts connections")
        except OSError:
            print(f"INFO: {label}: port {port} is not accepting connections")

    docker = shutil.which("docker", path=env["PATH"])
    if docker:
        try:
            result = subprocess.run(
                [docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=5,
                env=env,
            )
            print(
                f"{'OK' if result.returncode == 0 else 'WARN'}: Docker: "
                f"{result.stdout.strip() if result.returncode == 0 else 'daemon unavailable (optional for native development)'}"
            )
        except (OSError, subprocess.TimeoutExpired):
            print("WARN: Docker is unavailable (optional for native development)")
    else:
        print("WARN: Docker CLI is missing (optional for native development)")
    print(f"\n{'Ready' if not errors else 'Fix dependency/interpreter errors'}: {errors} error(s)")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
