import subprocess, os, yaml, time, signal, sys
from pathlib import Path
from contextlib import suppress

PX4_ENV = {
    "PX4_SIM_MODEL": "none",
    "PX4_HOME_LAT": "43.0747",
    "PX4_HOME_LON": "-89.3842",
    "PX4_HOME_ALT": "270",
}

def build_px4(px4_dir: Path):
    print("[PX4] Building base SITL once ...")
    subprocess.run(
        ["make", "px4_sitl", "CMAKE_CXX_STANDARD=17", "EXTRA_CXX_FLAGS=-Wno-error=double-promotion"],
        cwd=px4_dir,
        check=True
    )
    print("[PX4] Build complete")


def launch_px4(px4_dir: Path, drone_id: str, instance: int):
    build_dir = px4_dir / "build/px4_sitl_default"
    rootfs = build_dir / f"rootfs_{instance}"
    os.makedirs(rootfs, exist_ok=True)

    env = os.environ.copy()
    env.update(PX4_ENV)
    env["PX4_INSTANCE"] = str(instance)
    env["PX4_SIM_INSTANCE"] = str(instance)
    env["PX4_MAVLINK_UDP_PORT"] = str(14540 + instance)
    env["PX4_LOG_DIR"] = str(rootfs / "log")

    cmd = [
        str(build_dir / "bin/px4"),
        "-i", str(instance),
        "-d", str(rootfs),
        "-s", "etc/init.d-posix/rcS",
    ]

    print(f"[PX4] start {drone_id}: UDP {14540+instance}, ROOT={rootfs}")
    return subprocess.Popen(cmd, cwd=build_dir, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

def main():
    cfg_path = Path(__file__).parent / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    drones = cfg.get("drones", [])
    px4_dir = Path(__file__).parents[2] / drones[0]["px4_path"]

    # одноразовая сборка
    build_px4(px4_dir)

    procs = []
    try:
        for i, d in enumerate(drones):
            p = launch_px4(px4_dir, d["id"], i)
            procs.append((d["id"], p))
            time.sleep(2)
        print("[PX4 Launcher] All PX4 instances running (Ctrl+C to stop)")
        while True:
            for id_, p in procs.copy():
                if p.poll() is not None:
                    print(f"[PX4] {id_} exited with {p.returncode}")
                    procs.remove((id_, p))
                    continue
                line = p.stdout.readline()
                if line:
                    sys.stdout.write(f"[{id_}] {line}")
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[PX4 Launcher] Stopping...")
    finally:
        for _, p in procs:
            with suppress(Exception): p.send_signal(signal.SIGINT)
        for _, p in procs:
            with suppress(Exception): p.wait(timeout=5)

if __name__ == "__main__":
    main()
