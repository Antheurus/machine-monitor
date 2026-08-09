#!/usr/bin/env python3
"""Render the dashboard under conditions this machine is not currently in.

Everything else is verified against the machine as it happens to be right now,
which means the code paths for a hot, full, or broken machine are exactly the
ones never exercised — and those are the paths that matter, because that is when
someone actually opens this tool.

Each scenario builds a synthetic snapshot and renders it for real, then asserts
on the rendered text: thresholds produce the right colour, alerts fire, missing
sources degrade instead of crashing, and nothing overflows the terminal.

Run: python3 tests/eval_scenarios.py [--show SCENARIO]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import collect  # noqa: E402
import config as config_mod  # noqa: E402
import main as app  # noqa: E402
import render  # noqa: E402
import watch  # noqa: E402
from render import Theme  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
RED = "\033[38;5;196m"
ORANGE = "\033[38;5;208m"
GREEN = "\033[38;5;148m"

WIDTH = 120


def base_snapshot() -> dict:
    """A healthy machine. Every scenario is this with something turned bad."""
    machine = collect.Machine(
        model="Mac14,9", chip="M2 Pro", cores_total=10, cores_perf=6, cores_eff=4,
        ram_bytes=16 * 2**30, os_version="26.5.1", uptime_seconds=3600 * 30,
    )
    mem = collect.Memory(
        total=16 * 2**30, wired=3 * 2**30, active=3 * 2**30, inactive=2 * 2**30,
        compressed=1 * 2**30, free=6 * 2**30, used=7 * 2**30,
        swap_total=4 * 2**30, swap_used=0, page_size=16384, available=True,
    )
    disk = collect.Disk(total=460 * 2**30, free=300 * 2**30)
    procs = [
        collect.Process(pid=100 + i, name=f"proc{i}", rss=(20 - i) * 2**20,
                        footprint=(40 - i) * 2**20, cpu=float(20 - i),
                        age_seconds=3600.0 * (i + 1), ppid=1)
        for i in range(14)
    ]
    listeners = [
        collect.Listener(port=3000, pid=100, proto="TCP", name="node",
                         cwd="projects/web", age_seconds=7200, rss=50 * 2**20,
                         label="Node dev", is_project=True),
    ]
    return {
        "machine": machine, "memory": mem, "disk": disk,
        "battery": collect.Battery(percent=80, state="charging", cycles=404, health_pct=92),
        "cores": [12.0] * 10, "processes": procs, "listeners": listeners,
        "containers": [], "network": {}, "diskio": {},
        "temperatures": {"cpu": 45.0, "gpu": 44.0, "battery": 31.0, "ssd": 33.0},
        "thermal": collect.Thermal(pressure="Nominal", cpu_mw=800.0, gpu_mw=500.0),
        "alerts": [], "loadavg": (2.0, 2.0, 2.0), "trends": {}, "windowserver": [],
    }


def with_alerts(snap: dict, cfg: dict) -> dict:
    """Recompute the derived alert list so scenarios cannot forget to."""
    snap["alerts"] = collect.attention(
        snap["memory"], snap["disk"], snap["listeners"], snap["processes"], cfg
    )
    return snap


# ── scenarios ────────────────────────────────────────────────────────────────


def scenario_healthy(cfg):
    return base_snapshot()


def scenario_hot(cfg):
    snap = base_snapshot()
    snap["temperatures"] = {"cpu": 97.0, "gpu": 94.0, "battery": 44.0, "ssd": 71.0}
    snap["thermal"] = collect.Thermal(pressure="Critical", cpu_mw=24500.0, gpu_mw=18200.0)
    snap["cores"] = [100.0] * 10
    return snap


def scenario_memory_full(cfg):
    snap = base_snapshot()
    mem = snap["memory"]
    mem.wired = 5 * 2**30
    mem.active = 4 * 2**30
    mem.compressed = 6 * 2**30
    mem.used = 15 * 2**30 + 800 * 2**20
    mem.free = 100 * 2**20
    mem.swap_used = mem.swap_total
    return snap


def scenario_disk_full(cfg):
    snap = base_snapshot()
    snap["disk"] = collect.Disk(total=460 * 2**30, free=int(1.5 * 2**30))
    return snap


def scenario_cpu_pinned(cfg):
    snap = base_snapshot()
    snap["cores"] = [100.0] * 10
    snap["loadavg"] = (24.5, 20.1, 15.0)
    for index, proc in enumerate(snap["processes"][:4]):
        proc.cpu = 780.0 - index * 100  # a multi-threaded process exceeds 100%
    return snap


def scenario_no_sensors(cfg):
    """IOHID unavailable — e.g. an Intel Mac, or a future macOS that closes it."""
    snap = base_snapshot()
    snap["temperatures"] = {}
    snap["thermal"] = collect.Thermal(sudo_hint=True)
    return snap


def scenario_no_memory_source(cfg):
    snap = base_snapshot()
    snap["memory"] = collect.Memory()  # available=False, every field zero
    return snap


def scenario_empty_machine(cfg):
    snap = base_snapshot()
    snap["listeners"] = []
    snap["containers"] = []
    snap["processes"] = []
    snap["cores"] = []
    return snap


def scenario_swap_thrash(cfg):
    """The condition this machine is actually in: RSS tiny, footprint huge."""
    snap = base_snapshot()
    snap["memory"].swap_used = int(snap["memory"].swap_total * 0.94)
    snap["memory"].compressed = 6 * 2**30
    snap["memory"].used = 13 * 2**30
    for proc in snap["processes"][:3]:
        proc.rss = 3 * 2**20
        proc.footprint = 2 * 2**30
    return snap


def scenario_huge_values(cfg):
    """Absurd but possible numbers, to prove no column silently overflows."""
    snap = base_snapshot()
    snap["machine"].ram_bytes = 512 * 2**30
    snap["disk"] = collect.Disk(total=8 * 2**40, free=2 * 2**40)
    snap["listeners"] = [
        collect.Listener(
            port=65535, pid=999999, proto="TCP",
            name="a-process-with-an-extremely-long-executable-name-that-keeps-going",
            cwd="some/deeply/nested/path/that/will/not/fit/in/any/reasonable/column/width",
            age_seconds=86400 * 365, rss=64 * 2**30, label="Something", is_project=True,
        )
    ]
    snap["processes"][0].footprint = 400 * 2**30
    snap["processes"][0].cpu = 1599.9
    return snap


def scenario_shared_helper(cfg):
    """A framework XPC helper whose executable name names no app at all."""
    snap = base_snapshot()
    snap["processes"].append(collect.Process(
        pid=37870, name="com.apple.Virtualization.VirtualMachine", owner="Docker",
        rss=654 * 2**20, footprint=4105 * 2**20, cpu=10.9,
        age_seconds=421000.0, ppid=1,
    ))
    return snap


SCENARIOS = {
    "healthy": scenario_healthy,
    "hot": scenario_hot,
    "memory-full": scenario_memory_full,
    "swap-thrash": scenario_swap_thrash,
    "disk-full": scenario_disk_full,
    "cpu-pinned": scenario_cpu_pinned,
    "no-sensors": scenario_no_sensors,
    "no-memory-source": scenario_no_memory_source,
    "empty-machine": scenario_empty_machine,
    "huge-values": scenario_huge_values,
    "shared-helper": scenario_shared_helper,
}


# ── expectations ─────────────────────────────────────────────────────────────


def check(name: str, colored: str, plain: str, cfg: dict, snap: dict) -> list[str]:
    """Return a list of failures for one scenario."""
    problems = []

    for line in plain.splitlines():
        if len(line) > WIDTH:
            problems.append(f"line overflows {WIDTH} cols ({len(line)}): {line[:60]!r}")
            break

    def red_line(needle: str) -> bool:
        return any(needle in ln and RED in ln for ln in colored.splitlines())

    def orange_or_red_line(needle: str) -> bool:
        return any(needle in ln and (RED in ln or ORANGE in ln) for ln in colored.splitlines())

    if name == "healthy":
        if "NEEDS ATTENTION" in plain and "nothing worth acting on" not in plain:
            problems.append("healthy machine raised an alert")
        if not any("CPU die" in ln and GREEN in ln for ln in colored.splitlines()):
            problems.append("45C should render green")

    if name == "hot":
        if not red_line("CPU die"):
            problems.append("97C must render red")
        if "Critical" not in plain:
            problems.append("Critical thermal pressure not shown")
        if not watch.evaluate(snap, cfg):
            problems.append("hot machine produced no watch notice")

    if name in ("memory-full", "swap-thrash"):
        if not red_line("SWAP"):
            problems.append("swap at/near 100% must render red")
        if not any(a.severity == "crit" for a in snap["alerts"]):
            problems.append("no critical alert for exhausted swap")
        keys = {n.key for n in watch.evaluate(snap, cfg)}
        if "swap" not in keys:
            problems.append(f"watch did not raise swap (got {keys})")

    if name == "disk-full":
        if not red_line("DISK"):
            problems.append("99% disk must render red")
        if "disk" not in {n.key for n in watch.evaluate(snap, cfg)}:
            problems.append("watch did not raise disk")

    if name == "cpu-pinned":
        if not orange_or_red_line("CPU  "):
            problems.append("100% CPU must not render green")
        if "780" not in plain and "780.0" not in plain:
            problems.append("a >100% per-process CPU value was lost")

    if name == "no-sensors":
        if "°C" in plain:
            problems.append("temperatures shown when no sensor was available")
        if "powermetrics" not in plain:
            problems.append("no hint about enabling powermetrics")

    if name == "no-memory-source":
        # Match the vitals row by its shape — label plus a gauge — not by the
        # bare word: "MEM" is also a column heading in two tables, and a
        # substring test reported a defect that was not there.
        if any(re.match(r"\s+MEM\s+[█░]", ln) for ln in plain.splitlines()):
            problems.append("memory row rendered from an unavailable source")
        if any(re.match(r"\s+SWAP\s+[█░]", ln) for ln in plain.splitlines()):
            problems.append("swap row rendered from an unavailable source")

    if name == "empty-machine":
        for marker in ("nothing is listening",):
            if marker not in plain:
                problems.append(f"missing empty-state text: {marker!r}")

    if name == "huge-values":
        if "65535" not in plain:
            problems.append("a max-value port was dropped")

    if name == "shared-helper":
        helper = next(p for p in snap["processes"] if p.pid == 37870)
        # The owner leads the name precisely so it survives column truncation.
        if "Docker · com.apple.Virtualization" not in plain:
            problems.append("a shared helper rendered without its owning app")
        # The kill path re-reads `ps comm=` and compares it to this field, so a
        # decorated name here would refuse every kill as if the pid were reused.
        if helper.name != "com.apple.Virtualization.VirtualMachine":
            problems.append(f"Process.name was rewritten to {helper.name!r}")
        top_ram = plain.split("TOP RAM")[-1].splitlines()
        first = next((ln for ln in top_ram if "37870" in ln), "")
        if not first or first.strip() != next(ln.strip() for ln in top_ram if ln.strip()
                                              and ln.strip()[0].isdigit()):
            problems.append("a 4G footprint did not rank first in TOP RAM")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", metavar="SCENARIO", help="print one scenario's rendering")
    parser.add_argument("--list", action="store_true", help="list scenario names")
    args = parser.parse_args()

    if args.list:
        for name in SCENARIOS:
            print(name)
        return 0

    cfg = config_mod.load()
    cfg["_ram_bytes"] = 16 * 2**30
    cfg["sections"] = {name: True for name in app.SECTION_ORDER}

    if args.show:
        if args.show not in SCENARIOS:
            print(f"unknown scenario. choose from: {', '.join(SCENARIOS)}")
            return 2
        snap = with_alerts(SCENARIOS[args.show](cfg), cfg)
        print(app.build(snap, cfg, Theme(True), "once"))
        return 0

    failures = 0
    for name, builder in SCENARIOS.items():
        snap = with_alerts(builder(cfg), cfg)
        try:
            colored = app.build(snap, cfg, Theme(True), "once")
            plain = ANSI.sub("", app.build(snap, cfg, Theme(False), "once"))
        except Exception as exc:  # a render crash is the worst outcome; name it
            print(f"FAIL {name}: render raised {type(exc).__name__}: {exc}")
            failures += 1
            continue

        problems = check(name, colored, plain, cfg, snap)
        if problems:
            failures += 1
            print(f"FAIL {name}")
            for problem in problems:
                print(f"     {problem}")
        else:
            print(f"ok   {name:<18} {len(plain.splitlines()):>3} lines rendered")

    print(f"\n{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
