#!/usr/bin/env python3
"""Render the dashboard against a fictional machine, for README media.

    python3 tests/demo_render.py            # one full-colour frame
    python3 tests/demo_render.py --loop 60  # redraw every second for 60s

The real dashboard reads this machine, so a screen recording of it publishes
whatever the operator happens to be running -- project directory names, docker
container names and automation session names all reach the frame. This builds a
plausible machine instead, so the recording shows the tool rather than its
author's clients.

Shares the renderer with eval_scenarios.py; the numbers here are chosen to light
up every section, including the two that only appear under load.
"""

import argparse
import math
import os
import pathlib
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import collect  # noqa: E402
import config as config_mod  # noqa: E402
import main as app  # noqa: E402
from main import Theme  # noqa: E402

GB = 2**30
MB = 2**20


def snapshot(tick: int = 0) -> dict:
    """One frame. `tick` advances the live figures so a recording is not frozen.

    Only the values a real machine moves between samples are animated -- core
    load, per-process CPU, throughput, die temperature. Ports, container names
    and ages stay put, because those are what the viewer is reading.
    """
    rng = random.Random(tick * 7919)
    wave = math.sin(tick / 3.0)

    machine = collect.Machine(
        model="Mac14,9", chip="M2 Pro", cores_total=10, cores_perf=6, cores_eff=4,
        ram_bytes=16 * GB, os_version="26.5.1 (25F80)", uptime_seconds=3600 * 205,
    )
    mem = collect.Memory(
        total=16 * GB, wired=3 * GB + 800 * MB, active=2 * GB + 400 * MB,
        inactive=2 * GB + 400 * MB, compressed=6 * GB + 700 * MB,
        free=700 * MB, used=13 * GB, swap_total=16 * GB,
        swap_used=15 * GB + 500 * MB, page_size=16384, available=True,
    )

    listeners = [
        collect.Listener(port=3066, pid=38449, proto="TCP", name="node",
                         cwd="acme-storefront/frontend", age_seconds=36300,
                         rss=16 * MB, is_project=True),
        collect.Listener(port=3210, pid=92006, proto="TCP", name="next-server (v16.2.12)",
                         cwd="northwind-admin", age_seconds=239400, rss=15 * MB,
                         is_project=True),
        collect.Listener(port=3410, pid=2881, proto="TCP", name="bun",
                         cwd="orbit-dashboard", age_seconds=151200, rss=1 * MB,
                         is_project=True),
        collect.Listener(port=3412, pid=32153, proto="TCP", name="bun",
                         cwd="helios-gateway/backend", age_seconds=77500, rss=3 * MB,
                         is_project=True),
        collect.Listener(port=3510, pid=98779, proto="TCP", name="node",
                         cwd="orbit-dashboard/apps/web", age_seconds=151200, rss=5 * MB,
                         is_project=True),
        collect.Listener(port=5432, pid=602, proto="TCP", name="postgres",
                         cwd="/opt/homebrew/var/postgresql@16", age_seconds=738000,
                         rss=2 * MB, label="PostgreSQL"),
        collect.Listener(port=5406, pid=68456, proto="TCP", name="com.docker.backend",
                         cwd="~/Library/Containers/com.docker.docker/Data",
                         age_seconds=158400, rss=78 * MB),
        collect.Listener(port=6422, pid=68456, proto="TCP", name="com.docker.backend",
                         cwd="~/Library/Containers/com.docker.docker/Data",
                         age_seconds=158400, rss=78 * MB),
        collect.Listener(port=8066, pid=82715, proto="TCP", name="pike-api",
                         cwd="acme-storefront/backend", age_seconds=34600, rss=7 * MB,
                         is_project=True),
        collect.Listener(port=8425, pid=27470, proto="TCP", name="Python",
                         cwd="atlas-scraper", age_seconds=496800, rss=3 * MB,
                         is_project=True),
        collect.Listener(port=5353, pid=72524, proto="UDP", name="Google Chrome Helper",
                         cwd="/", label="mDNS", age_seconds=404000, rss=63 * MB),
        collect.Listener(port=9222, pid=66437, proto="TCP", name="Python",
                         cwd="atlas-scraper", age_seconds=213, rss=16 * MB,
                         is_project=True),
    ]

    containers = [
        collect.Container(name="acme-postgres", image="postgres:16-alpine",
                          status="Up 24 hours (healthy)", cpu="0.05%",
                          mem="20.42MiB / 3.826GiB"),
        collect.Container(name="orbit-redis", image="redis:7-alpine",
                          status="Up 24 hours", cpu="1.13%", mem="15.27MiB / 3.826GiB"),
        collect.Container(name="northwind-minio", image="minio/minio:latest",
                          status="Up 44 hours (healthy)", cpu="0.01%",
                          mem="197.5MiB / 512MiB"),
        collect.Container(name="helios-postgres", image="postgres:18-alpine",
                          status="Up 44 hours", cpu="0.00%", mem="50.76MiB / 3.826GiB"),
        collect.Container(name="atlas-worker", image="atlas-worker",
                          status="Up 42 hours", cpu="0.21%", mem="136.1MiB / 512MiB"),
        collect.Container(name="pike-gateway-postgres", image="postgres:16-alpine",
                          status="Up 44 hours (healthy)", cpu="0.00%",
                          mem="28.52MiB / 3.826GiB"),
    ]

    procs = [
        collect.Process(pid=68595, name="Docker · com.apple.Virtualization.VirtualMachine",
                        rss=282 * MB, footprint=4 * GB, cpu=54.0 + 9 * wave,
                        age_seconds=158400, ppid=1),
        collect.Process(pid=64989, name="Google Chrome Helper (Renderer)",
                        rss=155 * MB, footprint=109 * MB, cpu=17.9 + rng.uniform(-4, 6),
                        age_seconds=313, ppid=72512),
        collect.Process(pid=164, name="WindowServer", rss=43 * MB, footprint=1 * GB + 100 * MB,
                        cpu=16.1 + rng.uniform(-1.5, 2.5), age_seconds=738000, ppid=1),
        collect.Process(pid=43554, name="Maccy", rss=23 * MB, footprint=3 * GB + 900 * MB,
                        cpu=0.0, age_seconds=400000, ppid=1),
        collect.Process(pid=2881, name="bun", rss=1 * MB, footprint=2 * GB + 100 * MB,
                        cpu=0.0, age_seconds=151200, ppid=1),
        collect.Process(pid=81500, name="ollama", rss=167 * MB, footprint=988 * MB,
                        cpu=10.8, age_seconds=610000, ppid=1),
        collect.Process(pid=176, name="coreaudiod", rss=19 * MB, footprint=34 * MB,
                        cpu=9.0, age_seconds=738000, ppid=1),
        collect.Process(pid=72512, name="Google Chrome", rss=237 * MB, footprint=824 * MB,
                        cpu=1.8, age_seconds=404000, ppid=1, children=51),
        collect.Process(pid=68456, name="com.docker.backend", rss=78 * MB, footprint=135 * MB,
                        cpu=7.2, age_seconds=158400, ppid=1),
        collect.Process(pid=38449, name="node", rss=16 * MB, footprint=902 * MB,
                        cpu=0.0, age_seconds=36300, ppid=1, children=4),
        collect.Process(pid=445, name="Finder", rss=36 * MB, footprint=384 * MB,
                        cpu=5.4, age_seconds=738000, ppid=1),
        collect.Process(pid=72523, name="Google Chrome Helper", rss=50 * MB, footprint=923 * MB,
                        cpu=0.0, age_seconds=404000, ppid=72512),
    ]

    windowserver = [
        collect.Finding(factor="Refresh rate", status="1512 x 982 @ 60.00Hz",
                        verdict="reported rate is static, not the live adaptive rate",
                        severity="ok"),
        collect.Finding(factor="Video decode", status="2 VTDecoder instance(s)",
                        verdict="CAUSE: an app or browser is decoding video",
                        severity="cause"),
        collect.Finding(factor="Transparency", status="ON",
                        verdict="FACTOR: blur/vibrancy layers", severity="factor"),
        collect.Finding(factor="Displays", status="1 (internal)", verdict="",
                        severity="ok"),
    ]

    return {
        "machine": machine,
        "memory": mem,
        "disk": collect.Disk(total=460 * GB, free=52 * GB),
        "battery": collect.Battery(percent=95, state="discharging", cycles=439, health_pct=85),
        "cores": [max(3.0, min(99.0, base + rng.uniform(-14, 14) + 6 * wave))
                  for base in (31.0, 24.0, 22.0, 26.0, 63.0, 52.0, 44.0, 61.0, 41.0, 55.0)],
        "processes": procs,
        "listeners": listeners,
        "containers": containers,
        "network": {"en0": collect.Rate(
            in_total=54 * GB, out_total=70 * GB, in_rate=rng.uniform(1.2, 340) * 1024,
            out_rate=rng.uniform(0.8, 90) * 1024, label="en0", primed=True)},
        "diskio": {"disk0": collect.Rate(
            in_total=0, out_total=0, in_rate=rng.uniform(2, 46) * MB, out_rate=0,
            label="disk0", primed=True)},
        "temperatures": {"cpu": 42.2 + 2.4 * wave, "gpu": 44.8 + 1.8 * wave,
                         "battery": 32.2, "ssd": 35.0 + rng.uniform(-0.4, 0.4)},
        "thermal": collect.Thermal(pressure="Nominal",
                                   cpu_mw=1830.0 + 620 * wave, gpu_mw=1140.0 + 380 * wave),
        "alerts": [],
        "loadavg": (5.10 + 0.4 * wave, 3.96, 3.08),
        "trends": {},
        "windowserver": windowserver,
    }


def build(cfg: dict, tick: int = 0) -> str:
    snap = snapshot(tick)
    snap["alerts"] = collect.attention(
        snap["memory"], snap["disk"], snap["listeners"], snap["processes"], cfg
    )
    return app.build(snap, cfg, Theme(True), "once")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", type=int, metavar="SECONDS", default=0,
                        help="redraw every second for SECONDS, for a screen recording")
    parser.add_argument("--tick", type=int, default=0,
                        help="render one specific animation frame and exit")
    args = parser.parse_args()

    cfg = config_mod.load()
    cfg["_ram_bytes"] = 16 * GB
    cfg["sections"] = {name: True for name in app.SECTION_ORDER}
    # The footer prints this verbatim, and the real one carries the operator's
    # home directory into every frame of the recording.
    config_mod.CONFIG_PATH = pathlib.Path("~/.config/machine-monitor/config.ini")

    if not args.loop:
        print(build(cfg, args.tick))
        return 0

    deadline, tick = time.time() + args.loop, 0
    try:
        while time.time() < deadline:
            sys.stdout.write("\033[H\033[2J" + build(cfg, tick) + "\n")
            sys.stdout.flush()
            tick += 1
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
