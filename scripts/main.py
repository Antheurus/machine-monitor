#!/usr/bin/env python3
"""machine-monitor — live macOS machine dashboard.

Zero dependencies: system python3 only. Run with --help for options.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import select
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import actions  # noqa: E402
import clones as clones_mod  # noqa: E402
import collect  # noqa: E402
import config as config_mod  # noqa: E402
import history as history_mod  # noqa: E402
import render  # noqa: E402
import watch  # noqa: E402
from render import Canvas, Column, Theme, human_bytes, human_duration, pad  # noqa: E402

try:
    import termios
    import tty
except ImportError:  # not a POSIX terminal
    termios = None
    tty = None


# ── snapshot ─────────────────────────────────────────────────────────────────


class Monitor:
    """Holds the samplers whose values are deltas and therefore need state."""

    # Seconds a collector's result stays usable. Not every metric changes at the
    # same rate, and collecting them all at the fastest one's cadence is what
    # made a frame cost 2.7s: `docker stats` alone is 1.8s to re-read a container
    # list that changes maybe once an hour. 0 means every frame.
    TTL = {
        "containers": 30.0,
        "battery": 30.0,
        "disk": 20.0,
        "listeners": 8.0,
        "windowserver": 15.0,
        "footprints": 4.0,
        "thermal": 5.0,
        "memory": 0.0,
    }

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.machine = collect.machine_info()
        self.cores = collect.CoreSampler()
        self.processes = collect.ProcessSampler()
        self.temps = collect.TemperatureReader()
        self.network = collect.NetworkSampler()
        self.diskio = collect.DiskIOSampler()
        # Prime the counter-based samplers now so even a single-shot run has two
        # readings to difference; without this the first frame can only report
        # lifetime totals and the rate line stays blank.
        self.network.sample()
        self.diskio.sample()
        self.history = history_mod.History()
        self._cache: dict[str, tuple[float, object]] = {}

    def _cached(self, name: str, fn) -> object:
        """Run fn, or reuse its last result while still inside its TTL."""
        ttl = self.TTL.get(name, 0.0)
        now = time.monotonic()
        hit = self._cache.get(name)
        if hit and ttl and now - hit[0] < ttl:
            return hit[1]
        value = fn()
        self._cache[name] = (now, value)
        return value

    def snapshot(self) -> dict:
        """Gather one frame.

        The slow collectors are all subprocess-bound and independent of each
        other, so they run concurrently: powermetrics alone takes ~1.7s and
        `docker stats` ~1s, which sequentially dominated the whole frame.
        """
        cfg = self.cfg
        sections = cfg["sections"]
        interval = float(cfg.get("cpu_sample_seconds", 0.5))

        jobs = {
            "listeners": collect.listeners,
            "memory": collect.memory,
            "battery": collect.battery,
            "disk": lambda: collect.disk_usage("/"),
            "footprints": collect.footprints,
            "containers": collect.containers if sections.get("docker") else list,
            "thermal": (lambda: collect.thermal(cfg)) if sections.get("thermal") else collect.Thermal,
        }
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {
                name: pool.submit(self._cached, name, fn) for name, fn in jobs.items()
            }
            # The CPU samplers must run on this thread: their value is a delta
            # over the wall time they are given, so they define the frame's pace.
            procs = self.processes.sample(interval)
            core_pct = self.cores.sample()
            if not core_pct:  # the first call only primes the tick counters
                time.sleep(0.15)
                core_pct = self.cores.sample()
            temps = self.temps.read() if sections.get("thermal") else {}
            results = {name: future.result() for name, future in futures.items()}

        # Sampled here rather than in the pool so the delta window is the whole
        # frame; measured across thread submission it is milliseconds and reads zero.
        network, diskio = self.network.sample(), self.diskio.sample()

        prints = results["footprints"]
        for proc in procs:
            proc.footprint = prints.get(proc.pid, 0)
        collect.resolve_owners(procs)

        by_pid = {p.pid: p for p in procs}
        roots = cfg.get("project_roots", [])
        hidden = set(cfg.get("hide_ports", []))
        listener_list = results["listeners"]
        cwds = collect.cwd_for_pids(sorted({item.pid for item in listener_list}))

        visible: list[collect.Listener] = []
        for item in listener_list:
            if item.port in hidden:
                continue
            proc = by_pid.get(item.pid)
            if proc:
                item.name = proc.name
                item.age_seconds = proc.age_seconds
                item.rss = proc.rss
            item.cwd, item.is_project = collect.shorten_path(cwds.get(item.pid, ""), roots)
            item.label = config_mod.label_for_port(item.port, cfg)
            visible.append(item)

        collect.mark_session_owned(visible, cfg)
        stacks = collect.detached_stacks(procs, cfg, {item.pid for item in listener_list})

        # Collected here rather than inside the render: the diagnosis shells out
        # to system_profiler and ps, and running that from a draw function put
        # 40 subprocess spawns behind every keypress.
        windowserver = []
        if sections.get("windowserver"):
            ws = next((p for p in procs if p.name == "WindowServer"), None)
            if ws and ws.cpu >= cfg["thresholds"].get("windowserver_cpu", 15):
                windowserver = self._cached(
                    "windowserver",
                    lambda: collect.windowserver_diagnosis(
                        collect.run(["ps", "-eo", "pid=,comm="], timeout=6)
                    ),
                )

        mem, disk = results["memory"], results["disk"]
        wan = max(
            (r for name, r in network.items() if not name.startswith("lo")),
            key=lambda r: r.in_rate, default=None,
        )
        self.history.record({
            "cpu": sum(core_pct) / len(core_pct) if core_pct else None,
            "mem": mem.used_pct if mem.available else None,
            "swap": mem.swap_pct if mem.swap_total else None,
            "disk": disk.used_pct if disk.total else None,
            "temp_cpu": temps.get("cpu"),
            "load": os.getloadavg()[0],
            "net_in": wan.in_rate if wan else None,
            "net_out": wan.out_rate if wan else None,
        })

        return {
            "trends": {
                name: self.history.series(name, hours=1.0, points=48)
                for name in ("cpu", "mem", "swap", "disk")
            },
            "machine": self.machine,
            "memory": mem,
            "disk": disk,
            "battery": results["battery"],
            "cores": core_pct,
            "processes": procs,
            "listeners": visible,
            "containers": results["containers"],
            "network": network,
            "diskio": diskio,
            "temperatures": temps,
            "thermal": results["thermal"],
            "windowserver": windowserver,
            "stacks": stacks,
            "alerts": collect.attention(mem, disk, visible, procs, cfg, stacks=stacks),
            "loadavg": os.getloadavg(),
        }


class SnapshotFeed:
    """Gathers snapshots on a background thread and publishes the newest one.

    The live loop used to call snapshot() itself, which meant every keypress was
    answered only after the next full gather — pressing a sort key sat for
    seconds before anything moved. Collection latency and input latency are
    unrelated concerns and are now unrelated in the code: this thread owns the
    former, the render loop owns the latter.
    """

    def __init__(self, monitor: Monitor, interval: float) -> None:
        self._monitor = monitor
        self._interval = interval
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._generation = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self._monitor.snapshot()
            except Exception as exc:  # a collector fault must not kill the loop
                snap = {"_error": f"{type(exc).__name__}: {exc}"}
            with self._lock:
                self._latest = snap
                self._generation += 1
            self._wake.wait(self._interval)
            self._wake.clear()

    def start(self) -> None:
        self._thread.start()

    def refresh_now(self) -> None:
        self._wake.set()

    def latest(self) -> tuple[dict | None, int]:
        with self._lock:
            return self._latest, self._generation

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


# ── sections ─────────────────────────────────────────────────────────────────


def draw_header(c: Canvas, snap: dict, mode: str, view: "View | None" = None) -> None:
    t, m = c.t, snap["machine"]
    c.raw(t.paint("═" * c.width, t.divider))
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    left = (
        f"{t.bold}{t.title}  ⚡ MACHINE MONITOR{t.reset}"
        f"  {t.label}time{t.reset} {t.value}{now}{t.reset}"
        f"  {t.label}up{t.reset} {t.value}{human_duration(m.uptime_seconds)}{t.reset}"
    )
    right = (f"{t.dim}/ filter  c·m·a sort  k kill  q quit{t.reset}" if mode == "live"
             else f"{t.dim}live mode: -d{t.reset}")
    gap = c.width - render.visible_len(left) - render.visible_len(right) - 2
    c.raw(left + " " * max(1, gap) + right + " " if gap >= 1 else left)

    batt = snap["battery"]
    batt_str = f"{t.dim}n/a{t.reset}"
    if batt.percent is not None:
        color = t.level(100 - batt.percent, 60, 85)
        detail = f" {batt.state}" if batt.state else ""
        if batt.health_pct is not None:
            detail += f", health {batt.health_pct}%"
        if batt.cycles is not None:
            detail += f", {batt.cycles} cycles"
        batt_str = f"{color}{batt.percent}%{t.reset}{t.dim}{detail}{t.reset}"

    cores = f"{m.cores_total}"
    if m.cores_perf and m.cores_eff:
        cores += f" ({m.cores_perf}P+{m.cores_eff}E)"
    c.fields([
        f"{t.label}device{t.reset} {t.value}{m.model}{t.reset}",
        f"{t.label}chip{t.reset} {t.value}{m.chip}{t.reset}",
        f"{t.label}cores{t.reset} {t.value}{cores}{t.reset}",
        f"{t.label}ram{t.reset} {t.value}{human_bytes(m.ram_bytes)}{t.reset}",
        f"{t.label}macos{t.reset} {t.value}{m.os_version}{t.reset}",
        f"{t.label}batt{t.reset} {batt_str}",
    ])
    state = view.describe(t) if view else ""
    if state:
        c.fields([state, f"{t.dim}esc or 0 to clear{t.reset}"])
    c.raw(t.paint("═" * c.width, t.divider))


def draw_vitals(c: Canvas, snap: dict, cfg: dict) -> None:
    t = c.t
    th = cfg["thresholds"]
    c.section("VITALS")

    gw = 28 if c.width >= 110 else 14

    trends = snap.get("trends", {})
    spark_w = 16 if c.width >= 130 else 0

    def vital(name: str, pct: float, warn: float, crit: float, extras: list[str]) -> None:
        head = (
            f"{t.label}{pad(name, 6)}{t.reset}{c.gauge(pct, gw, warn, crit)} "
            f"{t.value}{pad(f'{pct:.0f}%', 5, 'right')}{t.reset}"
        )
        series = trends.get(name.lower())
        if spark_w and series:
            # Percentages share a fixed 0-100 ceiling so the shapes on adjacent
            # rows are comparable; an auto-scaled sparkline makes a flat 5% line
            # look identical to a flat 95% one.
            head += f" {t.dim}{render.sparkline(series, spark_w, ceiling=100.0)}{t.reset}"
        c.fields([head] + extras)

    cores = snap["cores"]
    m = snap["machine"]
    load1, load5, load15 = snap["loadavg"]
    if cores:
        overall = sum(cores) / len(cores)
        # Apple Silicon enumerates efficiency cores first.
        eff = cores[: m.cores_eff] if m.cores_eff else []
        perf = cores[m.cores_eff:] if m.cores_eff else cores
        extras = []
        if eff and perf:
            extras.append(
                f"{t.label}P{t.reset} {t.value}{sum(perf) / len(perf):.0f}%{t.reset}"
                f" {t.label}E{t.reset} {t.value}{sum(eff) / len(eff):.0f}%{t.reset}"
            )
        extras.append(
            f"{t.label}load{t.reset} {t.value}{load1:.2f} {load5:.2f} {load15:.2f}{t.reset}"
            f" {t.dim}({load1 / max(1, m.cores_total) * 100:.0f}% of {m.cores_total} cores){t.reset}"
        )
        vital("CPU", overall, th["cpu_warn"], th["cpu_crit"], extras)

        per_core = " ".join(
            f"{t.level(v, th['cpu_warn'], th['cpu_crit'])}{'▁▂▃▄▅▆▇█'[min(7, int(v / 12.5))]}{t.reset}"
            for v in cores
        )
        legend = f"{t.dim}E×{len(eff)} | P×{len(perf)}{t.reset}" if eff and perf else ""
        c.fields([f"{t.label}{pad('cores', 6)}{t.reset}{per_core}", legend])
    else:
        c.raw(f"  {t.label}{pad('CPU', 6)}{t.reset}{t.dim}per-core ticks unavailable{t.reset}")

    mem = snap["memory"]
    if mem.available:
        vital("MEM", mem.used_pct, th["mem_warn"], th["mem_crit"], [
            f"{t.value}{human_bytes(mem.used)}{t.reset}{t.label} of {human_bytes(mem.total)}{t.reset}",
            f"{t.dim}wired {human_bytes(mem.wired)} · active {human_bytes(mem.active)} · "
            f"compressed {human_bytes(mem.compressed)} · inactive {human_bytes(mem.inactive)}{t.reset}",
        ])
        if mem.swap_total:
            vital("SWAP", mem.swap_pct, th["swap_warn"], th["swap_crit"], [
                f"{t.value}{human_bytes(mem.swap_used)}{t.reset}{t.label} of "
                f"{human_bytes(mem.swap_total)}{t.reset}",
                f"{t.dim}RAM that no longer fits, parked on the SSD{t.reset}",
            ])

    disk = snap["disk"]
    if disk.total:
        vital("DISK", disk.used_pct, th["disk_warn"], th["disk_crit"], [
            f"{t.value}{human_bytes(disk.used)}{t.reset}{t.label} of {human_bytes(disk.total)}{t.reset}",
            f"{t.dim}{human_bytes(disk.free)} free{t.reset}",
        ])

    # Loopback carries local-only traffic and would otherwise win the "busiest"
    # pick on an idle machine, hiding the interface the user actually cares about.
    net = [r for name, r in snap["network"].items() if not name.startswith("lo")]
    busiest = max(net, key=lambda r: (r.in_rate + r.out_rate, r.in_total), default=None)
    if busiest is not None and busiest.primed:
        chunks = [
            f"{t.label}{pad('NET', 6)}{t.reset}{t.value}↓ {human_bytes(busiest.in_rate)}/s{t.reset}"
            f"  {t.value}↑ {human_bytes(busiest.out_rate)}/s{t.reset}",
            f"{t.dim}on {busiest.label}{t.reset}",
            f"{t.dim}lifetime {human_bytes(busiest.in_total)} in · "
            f"{human_bytes(busiest.out_total)} out{t.reset}",
        ]
        io = snap["diskio"]
        spinning = max(io.values(), key=lambda r: r.in_rate, default=None)
        if spinning is not None and spinning.primed:
            chunks.append(
                f"{t.label}disk{t.reset} {t.value}{human_bytes(spinning.in_rate)}/s{t.reset}"
                f" {t.dim}on {spinning.label}{t.reset}"
            )
        c.fields(chunks)


def draw_thermal(c: Canvas, snap: dict, cfg: dict) -> None:
    t = c.t
    th = cfg["thresholds"]
    temps = snap["temperatures"]
    therm = snap["thermal"]
    if not temps and not therm.pressure and not therm.sudo_hint:
        return

    c.section("THERMAL", "die temps read from IOHID sensors — no sudo, no install")
    labels = (("cpu", "CPU die"), ("gpu", "GPU"), ("battery", "Battery"), ("ssd", "SSD"))
    cells = []
    for key, label in labels:
        if key not in temps:
            continue
        value = temps[key]
        color = t.level(value, th["temp_warn"], th["temp_crit"])
        cells.append(f"{t.label}{label}{t.reset} {color}{value:.1f}°C{t.reset}")
    if cells:
        c.fields(cells)

    extra = []
    if therm.pressure:
        color = {"Nominal": t.ok, "Moderate": t.warn}.get(therm.pressure, t.crit)
        extra.append(f"{t.label}pressure{t.reset} {color}{therm.pressure}{t.reset}")
    if therm.cpu_mw is not None:
        extra.append(f"{t.label}CPU power{t.reset} {t.value}{therm.cpu_mw / 1000:.2f} W{t.reset}")
    if therm.gpu_mw is not None:
        extra.append(f"{t.label}GPU power{t.reset} {t.value}{therm.gpu_mw / 1000:.2f} W{t.reset}")
    if extra:
        c.fields(extra)
    if therm.sudo_hint:
        c.wrapped("thermal pressure and power draw need passwordless powermetrics:", t.dim)
        c.wrapped(
            'echo "$(whoami) ALL=(ALL) NOPASSWD: /usr/bin/powermetrics" '
            "| sudo tee /etc/sudoers.d/powermetrics", t.dim,
        )


def draw_servers(c: Canvas, snap: dict) -> None:
    t = c.t
    rows = snap["listeners"]
    c.section("SERVERS RUNNING", f"{len(rows)} listening socket(s)")
    if not rows:
        c.raw(f"  {t.dim}nothing is listening{t.reset}")
        return

    specs = [
        Column("port", "PORT", 7, "left", priority=9),
        Column("proto", "PROTO", 5, "left", priority=2),
        Column("pid", "PID", 7, "right", priority=9),
        Column("name", "PROCESS", 24, "left", priority=7),
        Column("age", "AGE", 8, "right", priority=4),
        Column("mem", "MEM", 7, "right", priority=3),
        Column("detail", "PROJECT / SERVICE", 24, "left", priority=8),
    ]
    body = []
    for item in rows:
        age = human_duration(item.age_seconds) if item.age_seconds else "-"
        body.append({
            "port": (str(item.port), t.port),
            "proto": (item.proto, t.dim),
            "pid": (str(item.pid), t.pid),
            "name": (item.name or "?", t.value),
            "age": (age, t.warn if item.age_seconds > 86400 else t.label),
            "mem": (human_bytes(item.rss) if item.rss else "-", t.info),
            "detail": (
                " · ".join(part for part in (item.cwd, item.label) if part),
                t.accent if item.is_project else t.dim,
            ),
        })
    c.table(specs, body)


def draw_containers(c: Canvas, snap: dict) -> None:
    rows = snap["containers"]
    if not rows:
        return
    t = c.t
    c.section("DOCKER CONTAINERS", f"{len(rows)} running")
    specs = [
        Column("name", "NAME", 28, "left", priority=9),
        Column("cpu", "CPU", 8, "right", priority=6),
        Column("mem", "MEM", 20, "left", priority=7),
        Column("status", "STATUS", 16, "left", priority=4),
        Column("image", "IMAGE", 20, "left", priority=3),
    ]
    c.table(specs, [{
        "name": (item.name, t.value),
        "cpu": (item.cpu or "-", t.good),
        "mem": (item.mem or "-", t.info),
        "status": (item.status, t.dim),
        "image": (item.image, t.dim),
    } for item in rows])


def _draw_process_table(c: Canvas, rows: list, cfg: dict, key: str) -> None:
    t = c.t
    th = cfg["thresholds"]
    bar_w = 22 if c.width >= 110 else 12
    specs = [
        Column("pid", "PID", 7, "right", priority=9),
        Column("cpu", "CPU%", 7, "right", priority=8),
        Column("mem", "MEM", 8, "right", priority=8),
        Column("rss", "RSS", 8, "right", priority=2),
        Column("age", "AGE", 8, "right", priority=4),
        Column("bar", "LOAD", bar_w, "left", priority=3),
        Column("name", "PROCESS", 24, "left", priority=9),
    ]
    total_ram = cfg["_ram_bytes"] or 1
    # The RAM bar is scaled against the biggest process in the table rather than
    # against total RAM: no single process comes near 100% of RAM, so an absolute
    # scale renders every row as one indistinguishable stub.
    peak_mem = max((p.memory for p in rows), default=1) or 1
    body = []
    for proc in rows:
        cpu_color = t.level(proc.cpu, th["cpu_warn"], th["cpu_crit"])
        mem_pct = 100.0 * proc.memory / total_ram
        mem_color = t.level(mem_pct, 3.0, 8.0)
        if key == "cpu":
            bar_pct, bar_color = min(100.0, proc.cpu), cpu_color
        else:
            bar_pct, bar_color = 100.0 * proc.memory / peak_mem, mem_color
        filled = max(0, min(bar_w, int(round(bar_pct / 100.0 * bar_w))))
        # A footprint far above RSS means most of this process has been
        # compressed or swapped out — it is a hog that ranking by RSS hides.
        swollen = proc.footprint and proc.rss and proc.footprint > proc.rss * 4
        body.append({
            "pid": (str(proc.pid), t.pid),
            "cpu": (f"{proc.cpu:.1f}", cpu_color),
            "mem": (human_bytes(proc.memory), mem_color),
            "rss": (human_bytes(proc.rss), t.warn if swollen else t.dim),
            "age": (human_duration(proc.age_seconds), t.dim),
            "bar": ("█" * filled + "░" * (bar_w - filled), bar_color),
            "name": (
                f"{proc.display_name} ×{proc.children + 1}"
                if proc.children > 2 else proc.display_name,
                t.value,
            ),
        })
    c.table(specs, body)


def draw_top_cpu(c: Canvas, snap: dict, cfg: dict) -> None:
    rows = sorted(snap["processes"], key=lambda p: p.cpu, reverse=True)[: cfg["top_processes"]]
    c.section("TOP CPU", f"live — sampled over {cfg['cpu_sample_seconds']}s, not a lifetime average")
    _draw_process_table(c, rows, cfg, "cpu")


def draw_top_ram(c: Canvas, snap: dict, cfg: dict) -> None:
    rows = sorted(snap["processes"], key=lambda p: p.memory, reverse=True)[: cfg["top_processes"]]
    c.section(
        "TOP RAM",
        "MEM is physical footprint, what Activity Monitor shows. "
        "An orange RSS far below it means the process has been compressed or swapped out.",
    )
    _draw_process_table(c, rows, cfg, "rss")


def draw_windowserver(c: Canvas, snap: dict, cfg: dict) -> None:
    t = c.t
    findings = snap.get("windowserver")
    if not findings:
        return
    ws = next((p for p in snap["processes"] if p.name == "WindowServer"), None)
    c.section(
        "WINDOWSERVER DIAGNOSIS",
        f"WindowServer at {ws.cpu:.1f}% — live checks" if ws else "live checks",
    )
    palette = {"cause": t.crit, "factor": t.warn, "ok": t.ok}
    marks = {"cause": "←", "factor": "←", "ok": "✓"}
    specs = [
        Column("factor", "FACTOR", 22, "left", priority=9),
        Column("status", "STATUS", 32, "left", priority=7),
        Column("verdict", "VERDICT", 24, "left", priority=8),
    ]
    c.table(specs, [{
        "factor": (f.factor, t.value),
        "status": (f.status, palette[f.severity]),
        "verdict": (
            f"{marks[f.severity]} {f.verdict}".strip(), palette[f.severity],
        ),
    } for f in findings])


def draw_attention(c: Canvas, snap: dict) -> None:
    alerts = snap["alerts"]
    t = c.t
    c.section("NEEDS ATTENTION")
    if not alerts:
        c.raw(f"  {t.ok}✓{t.reset} {t.dim}nothing worth acting on right now{t.reset}")
        return
    palette = {"crit": (t.crit, "⚠"), "warn": (t.warn, "⚠"), "info": (t.info, "·")}
    for alert in alerts:
        color, mark = palette[alert.severity]
        c.wrapped(f"{mark} {alert.text}", color)


def draw_space(c: Canvas, items: list, cfg: dict) -> None:
    t = c.t
    total = sum(item.bytes for item in items)
    disk = collect.disk_usage("/")
    c.section(
        "RECLAIMABLE SPACE",
        f"{human_bytes(total)} across {len(items)} locations — everything here rebuilds itself",
    )
    if not items:
        c.raw(f"  {t.ok}✓{t.reset} {t.dim}nothing large enough to bother with{t.reset}")
        return

    specs = [
        Column("size", "SIZE", 8, "right", priority=9),
        Column("kind", "KIND", 14, "left", priority=6),
        Column("note", "HOW TO CLEAR", 30, "left", priority=4),
        Column("path", "PATH", 30, "left", priority=9),
    ]
    c.table(specs, [{
        "size": (human_bytes(item.bytes), t.level(item.bytes / 2**30, 1.0, 4.0)),
        "kind": (item.kind, t.dim),
        "note": (item.note, t.accent if item.note else t.dim),
        "path": (collect.shorten_path(item.path, cfg.get("project_roots", []))[0] or item.path, t.value),
    } for item in items])

    if disk.total:
        after = disk.used_pct - 100.0 * total / disk.total
        c.raw("")
        # A floor, not a forecast: these sizes are du-style, and du bills an APFS
        # clone and a shared Docker layer at full size. See --clones.
        c.wrapped(
            f"Clearing all of it takes the disk from {disk.used_pct:.0f}% to "
            f"no better than about {after:.0f}%.",
            t.ok,
        )


def draw_clones(c: Canvas, groups: list) -> None:
    t = c.t
    total = sum(g.apparent_bytes for g in groups)
    count = sum(g.count for g in groups)
    c.section(
        "CODE-SIGN CLONE LITTER",
        f"{count} clone(s) across {len(groups)} app(s), {human_bytes(total)} apparent",
    )
    if not groups:
        c.raw(f"  {t.ok}✓{t.reset} {t.dim}nothing left behind{t.reset}")
        return

    now = time.time()
    specs = [
        Column("count", "CLONES", 6, "right", priority=9),
        Column("apparent", "APPARENT", 9, "right", priority=8),
        Column("busy", "IN USE", 6, "right", priority=6),
        Column("oldest", "OLDEST", 8, "right", priority=4),
        Column("app", "APP", 34, "left", priority=9),
    ]
    c.table(specs, [{
        "count": (str(g.count), t.level(g.count, 5, 25)),
        "apparent": (human_bytes(g.apparent_bytes), t.dim),
        "busy": ("?" if g.in_use is None else str(len(g.in_use)), t.dim),
        "oldest": (human_duration(now - g.oldest), t.dim),
        "app": (g.label, t.value),
    } for g in groups])

    c.raw("")
    c.wrapped(
        "APPARENT is what du and the Storage pane report, and it is an upper bound, "
        "not a forecast. These are APFS clones sharing blocks with the original app, "
        "so the disk gets back a fraction of it — 258 Chrome clones billed at 652 GB "
        "returned 2.4 GB. The real figure is measured from statvfs after removal.",
        t.warn,
    )
    if any(g.in_use is None for g in groups):
        c.wrapped(
            "IN USE '?' means lsof could not say which clones are open, so those "
            "buckets will be refused rather than guessed at.", t.warn,
        )
    c.wrapped("Clear with:  machine-monitor --clean-clones", t.accent)


def report_removals(reports: list) -> int:
    """Print what each bucket actually gave back, apparent beside real."""
    total_real = total_apparent = removed = 0
    planned = failed = 0

    for report in reports:
        group = report.group
        if group.in_use is None:
            print(f"  {group.label}: refused — lsof could not identify the clones in use")
            failed += 1
            continue
        if not report.results:
            print(f"  {group.label}: nothing removable ({group.count} clone(s), all in use)")
            continue

        planned += sum(1 for r in report.results if r.outcome == "planned")
        for result in report.results:
            if result.outcome in ("refused", "error"):
                failed += 1
                print(f"  {group.label}: {result.outcome} {result.path} — {result.detail}")

        removed += report.removed
        total_apparent += report.apparent_bytes
        total_real += report.real_bytes
        if report.removed:
            print(
                f"  {group.label}: removed {report.removed} clone(s), "
                f"{human_bytes(report.apparent_bytes)} apparent → "
                f"{human_bytes(report.real_bytes)} actually freed"
            )

    if planned:
        print(f"\ndry run: {planned} clone(s) would be removed, nothing was deleted")
        return 0
    if removed:
        print(
            f"\nremoved {removed} clone(s). "
            f"{human_bytes(total_apparent)} apparent, "
            f"{human_bytes(total_real)} real — free space measured either side, not estimated."
        )
    elif not failed:
        print("\nnothing to remove")
    return 1 if failed and not removed else 0


def draw_orphans(c: Canvas, orphans: list) -> None:
    t = c.t
    # One automation run is a parent browser plus its helpers, all sharing a
    # profile directory — the profile is the unit a person kills, not the pid.
    groups: dict[str, list] = {}
    for item in orphans:
        groups.setdefault(item.profile, []).append(item)

    total_mem = sum(item.memory for item in orphans)
    c.section(
        "ORPHANED AUTOMATION BROWSERS",
        f"{len(groups)} stuck session(s), {len(orphans)} processes, {human_bytes(total_mem)}",
    )
    if not groups:
        c.raw(f"  {t.ok}✓{t.reset} {t.dim}no leftover automation profiles{t.reset}")
        return

    specs = [
        Column("age", "AGE", 8, "right", priority=9),
        Column("procs", "PROCS", 6, "right", priority=7),
        Column("mem", "MEM", 8, "right", priority=8),
        Column("root", "ROOT PID", 9, "right", priority=6),
        Column("profile", "PROFILE", 30, "left", priority=9),
    ]
    rows = sorted(groups.items(), key=lambda kv: -max(i.age_seconds for i in kv[1]))
    c.table(specs, [{
        "age": (human_duration(max(i.age_seconds for i in members)), t.warn),
        "procs": (str(len(members)), t.value),
        "mem": (human_bytes(sum(i.memory for i in members)), t.info),
        "root": (str(min(i.pid for i in members)), t.pid),
        "profile": (profile, t.dim),
    } for profile, members in rows])
    c.raw("")
    c.wrapped("Kill a whole session with:  machine-monitor --kill-orphans", t.accent)


def sample_with_footprints(monitor) -> list:
    """A process sample whose MEM is the footprint, not RSS.

    `ProcessSampler` fills RSS only; the footprint is a separate `top` read that
    the full snapshot merges in. Every one-shot mode skipped that merge, so
    `Process.memory` silently fell back to RSS — the orphan view reported a
    2.70 G pile of leftover browsers as 1.2 G, and a compressed 2.2 G process as
    3.8 M. Any mode that shows or sums memory goes through here.
    """
    procs = monitor.processes.sample(0.3)
    prints = collect.footprints()
    for proc in procs:
        proc.footprint = prints.get(proc.pid, 0)
    return procs


def draw_sessions(c: Canvas, found: list, family: str = "") -> None:
    t = c.t
    total_procs = sum(len(s.pids) for s in found)
    total_mem = sum(s.memory for s in found)
    c.section(
        "PROCESS SESSIONS",
        f"{len(found)} session(s), {total_procs} processes, {human_bytes(total_mem)}"
        + (f" — family {family}" if family else ""),
    )
    if not found:
        c.raw(f"  {t.ok}✓{t.reset} {t.dim}no matching process families running{t.reset}")
        return

    specs = [
        Column("age", "AGE", 8, "right", priority=9),
        Column("procs", "PROCS", 6, "right", priority=7),
        Column("mem", "MEM", 8, "right", priority=8),
        Column("root", "ROOT PID", 9, "right", priority=9),
        Column("family", "FAMILY", 19, "left", priority=8),
        Column("detail", "SESSION", 30, "left", priority=9),
    ]
    c.table(specs, [{
        "age": (human_duration(s.age_seconds), t.warn if s.age_seconds > 86400 else t.label),
        "procs": (str(len(s.pids)), t.value),
        "mem": (human_bytes(s.memory), t.info),
        "root": (str(s.root_pid), t.pid),
        "family": (s.family, t.accent),
        "detail": (s.detail, t.dim),
    } for s in found])
    c.raw("")
    c.wrapped("Kill one session with:  machine-monitor --kill-session <ROOT PID>", t.accent)


def draw_history(c: Canvas, store, hours: float) -> None:
    t = c.t
    c.section("HISTORY", f"last {hours:g}h — recorded every time the dashboard runs")
    if not store.available:
        c.wrapped(f"history unavailable: {store.error}", t.warn)
        return

    labels = {
        "cpu": ("CPU", "%"), "mem": ("Memory", "%"), "swap": ("Swap", "%"),
        "disk": ("Disk", "%"), "temp_cpu": ("CPU temp", "°C"), "load": ("Load", ""),
    }
    rows = []
    for metric, (label, unit) in labels.items():
        trend = store.trend(metric, hours)
        if not trend or trend.samples < 2:
            continue
        arrow = "↑" if trend.change > 1 else ("↓" if trend.change < -1 else "→")
        color = t.warn if trend.change > 5 else (t.ok if trend.change < -5 else t.dim)
        rows.append((label, unit, trend, arrow, color))

    if not rows:
        c.wrapped(
            "Not enough samples yet. Every dashboard run records one; "
            "run it a few times, or leave live mode open.", t.dim,
        )
        return

    width = 30 if c.width >= 120 else 16
    for label, unit, trend, arrow, color in rows:
        series = store.series(trend.metric, hours=hours, points=width * 2)
        ceiling = 100.0 if unit == "%" else None
        c.fields([
            f"{t.label}{pad(label, 10)}{t.reset}{t.accent}"
            f"{render.sparkline(series, width, ceiling=ceiling)}{t.reset}",
            f"{t.value}{trend.current:.0f}{unit}{t.reset} {color}{arrow}{t.reset}"
            f" {t.dim}from {trend.oldest:.0f}{unit}{t.reset}",
            f"{t.dim}min {trend.minimum:.0f} · max {trend.maximum:.0f} · "
            f"{trend.samples} samples over {trend.hours:.1f}h{t.reset}",
        ])


def draw_diff(c: Canvas, name: str, changes: list) -> None:
    t = c.t
    c.section("SNAPSHOT DIFF", f"against saved snapshot '{name}'")
    if not changes:
        c.raw(f"  {t.ok}✓{t.reset} {t.dim}nothing moved enough to report{t.reset}")
        return
    palette = {"up": (t.warn, "↑"), "down": (t.ok, "↓"), "new": (t.info, "+"), "gone": (t.dim, "-")}
    specs = [
        Column("mark", "", 2, "left", priority=9),
        Column("label", "WHAT", 34, "left", priority=9),
        Column("before", "BEFORE", 14, "right", priority=8),
        Column("after", "AFTER", 14, "right", priority=8),
        Column("pad", "", 4, "left", priority=1),
    ]
    c.table(specs, [{
        "mark": (palette[ch.direction][1], palette[ch.direction][0]),
        "label": (ch.label, t.value),
        "before": (ch.before, t.dim),
        "after": (ch.after, palette[ch.direction][0]),
        "pad": ("", ""),
    } for ch in changes])


def draw_reclaim(c: Canvas, stale: list, hours: float) -> None:
    t = c.t
    total = sum(item.rss for item in stale)
    c.section(
        "STALE DEV SERVERS",
        f"{len(stale)} listening longer than {hours:g}h, holding {human_bytes(total)}",
    )
    if not stale:
        c.raw(f"  {t.ok}✓{t.reset} {t.dim}nothing has been running long enough to be forgotten{t.reset}")
        return
    specs = [
        Column("port", "PORT", 7, "left", priority=9),
        Column("pid", "PID", 7, "right", priority=9),
        Column("age", "AGE", 8, "right", priority=8),
        Column("mem", "MEM", 8, "right", priority=7),
        Column("name", "PROCESS", 22, "left", priority=6),
        Column("cwd", "PROJECT", 26, "left", priority=9),
    ]
    c.table(specs, [{
        "port": (str(item.port), t.port),
        "pid": (str(item.pid), t.pid),
        "age": (human_duration(item.age_seconds), t.warn),
        "mem": (human_bytes(item.rss), t.info),
        "name": (item.name, t.value),
        "cwd": (item.cwd, t.accent),
    } for item in sorted(stale, key=lambda i: -i.age_seconds)])


def draw_stacks(c: Canvas, stacks: list) -> None:
    t = c.t
    c.section(
        "DETACHED DEV STACKS",
        f"{len(stacks)} with no listening port, holding {human_bytes(sum(s.memory for s in stacks))}",
    )
    if not stacks:
        c.raw(f"  {t.ok}✓{t.reset} {t.dim}no dev tree outlived its session without a port{t.reset}")
        return
    specs = [
        Column("pid", "ROOT", 7, "right", priority=9),
        Column("procs", "PROCS", 6, "right", priority=7),
        Column("age", "AGE", 8, "right", priority=8),
        Column("mem", "MEM", 8, "right", priority=9),
        Column("cmd", "COMMAND", 28, "left", priority=6),
        Column("cwd", "PROJECT", 26, "left", priority=9),
    ]
    c.table(specs, [{
        "pid": (str(item.pid), t.pid),
        "procs": (str(len(item.pids)), t.dim),
        "age": (human_duration(item.age_seconds), t.warn),
        "mem": (human_bytes(item.memory), t.info),
        "cmd": (item.args, t.value),
        "cwd": (item.cwd, t.accent),
    } for item in stacks])


def confirm_destructive(count: int, what: str, assume_yes: bool,
                        noun: str = "process(es)") -> bool:
    """Require the word 'yes', typed, before anything is signalled.

    A y/n prompt is too easy to answer reflexively for an action with no undo,
    and a non-interactive run must never be able to fall through to a default —
    so without a tty this refuses and points at the explicit flag instead.
    """
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"\nrefusing to {what} {count} {noun} without a terminal — pass --yes to proceed")
        return False
    try:
        answer = input(f"\n{what} {count} {noun}? type 'yes' to confirm: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() == "yes"


def report_kills(results: list) -> int:
    planned = [r for r in results if r.outcome == "planned"]
    if planned:
        for result in planned:
            print(f"  {result.pid:>7} {result.name:<30} would be terminated")
        print(f"\ndry run: {len(planned)} process(es) would be terminated, none were signalled")
        return 0

    done = [r for r in results if r.outcome in ("terminated", "killed")]
    gone = [r for r in results if r.outcome == "gone"]
    bad = [r for r in results if r.outcome in ("refused", "error")]
    for result in results:
        if result.outcome in ("refused", "error"):
            print(f"  {result.pid:>7} {result.name:<30} {result.outcome}: {result.detail}")
    print(f"\n{len(done)} terminated, {len(gone)} already gone, {len(bad)} refused or failed")
    return 0 if not bad else 1


def draw_footer(c: Canvas, cfg: dict) -> None:
    t = c.t
    c.raw("")
    c.rule()
    c.fields([
        f"{t.label}kill by PID{t.reset} {t.kill}kill <PID>{t.reset}",
        f"{t.label}kill a port's listener{t.reset} "
        f"{t.kill}lsof -nP -iTCP:<PORT> -sTCP:LISTEN -t | xargs kill{t.reset}",
    ])
    c.wrapped(
        "-sTCP:LISTEN matters: a bare `lsof -ti:<port>` also matches CLIENT "
        "connections, so it can kill your browser instead of the server.", t.dim,
    )
    c.wrapped(f"config: {config_mod.CONFIG_PATH}", t.dim)
    if cfg.get("_config_error"):
        c.wrapped(f"config problem: {cfg['_config_error']}", t.warn)
    c.rule()


# ── composition ──────────────────────────────────────────────────────────────


SECTION_ORDER = (
    "vitals", "thermal", "attention", "servers", "docker", "top_cpu", "windowserver", "top_ram",
)

SORT_KEYS = {"c": ("cpu", "CPU"), "m": ("memory", "memory"), "a": ("age_seconds", "age")}


@dataclasses.dataclass
class View:
    """What the viewer has asked to see. Live mode only."""

    sort: str = ""
    filter: str = ""

    def matches(self, *fields: str) -> bool:
        if not self.filter:
            return True
        needle = self.filter.lower()
        return any(needle in (field or "").lower() for field in fields)

    def apply(self, snap: dict) -> dict:
        if not self.filter and not self.sort:
            return snap
        view = dict(snap)
        if self.filter:
            view["listeners"] = [
                item for item in snap["listeners"]
                if self.matches(item.name, item.cwd, item.label, str(item.port))
            ]
            view["processes"] = [
                proc for proc in snap["processes"] if self.matches(proc.name, str(proc.pid))
            ]
            view["containers"] = [
                item for item in snap["containers"] if self.matches(item.name, item.image)
            ]
        if self.sort:
            view["processes"] = sorted(
                view["processes"], key=lambda p: getattr(p, self.sort, 0), reverse=True
            )
        return view

    def describe(self, theme: Theme) -> str:
        parts = []
        if self.filter:
            parts.append(f"{theme.accent}filter “{self.filter}”{theme.reset}")
        if self.sort:
            label = next((n for k, (f, n) in SORT_KEYS.items() if f == self.sort), self.sort)
            parts.append(f"{theme.accent}sorted by {label}{theme.reset}")
        return "  ".join(parts)


def build(snap: dict, cfg: dict, theme: Theme, mode: str, view: "View | None" = None) -> str:
    c = Canvas(theme, render.term_width())
    sections = cfg["sections"]
    draw_header(c, snap, mode, view)
    if sections.get("vitals"):
        draw_vitals(c, snap, cfg)
    if sections.get("thermal"):
        draw_thermal(c, snap, cfg)
    if sections.get("attention"):
        draw_attention(c, snap)
    if sections.get("servers"):
        draw_servers(c, snap)
    if sections.get("docker"):
        draw_containers(c, snap)
    if sections.get("top_cpu"):
        draw_top_cpu(c, snap, cfg)
    if sections.get("windowserver"):
        draw_windowserver(c, snap, cfg)
    if sections.get("top_ram"):
        draw_top_ram(c, snap, cfg)
    draw_footer(c, cfg)
    return c.render()


def to_json(snap: dict) -> str:
    def encode(value):
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return dataclasses.asdict(value)
        raise TypeError(f"not serializable: {type(value)!r}")

    def proc(p: collect.Process) -> dict:
        row = dataclasses.asdict(p)
        row["cpu"] = round(p.cpu, 1)
        row["age_seconds"] = round(p.age_seconds)
        row["memory"] = p.memory
        return row

    payload = {
        "machine": dataclasses.asdict(snap["machine"]),
        "memory": dataclasses.asdict(snap["memory"]),
        "disk": {
            "total": snap["disk"].total,
            "free": snap["disk"].free,
            "used_pct": round(snap["disk"].used_pct, 1),
        },
        "battery": dataclasses.asdict(snap["battery"]),
        "cores": [round(v, 1) for v in snap["cores"]],
        "loadavg": list(snap["loadavg"]),
        "temperatures": snap["temperatures"],
        "thermal": dataclasses.asdict(snap["thermal"]),
        "network": {k: dataclasses.asdict(v) for k, v in snap["network"].items() if v.in_total},
        "diskio": {k: dataclasses.asdict(v) for k, v in snap["diskio"].items()},
        "listeners": [dataclasses.asdict(item) for item in snap["listeners"]],
        "containers": [dataclasses.asdict(item) for item in snap["containers"]],
        "alerts": [dataclasses.asdict(item) for item in snap["alerts"]],
        "detached_stacks": [dataclasses.asdict(item) for item in snap.get("stacks", [])],
        "top_cpu": [proc(p) for p in sorted(snap["processes"], key=lambda p: p.cpu, reverse=True)[:15]],
        # Ranked by footprint, matching the rendered table: RSS put the largest
        # consumer on this machine fourth, which is the whole reason MEM exists.
        "top_ram": [proc(p) for p in sorted(snap["processes"], key=lambda p: p.memory, reverse=True)[:15]],
    }
    return json.dumps(payload, indent=2, default=encode)


@contextlib.contextmanager
def kill_interactive(snap: dict, theme: Theme) -> None:
    """Kill a pid typed by the viewer, confirming with what that pid actually is.

    Takes a typed pid rather than a moving row cursor: a frame here takes seconds
    to gather, so a highlighted row would be several seconds stale by the time it
    was acted on — and the row under the cursor is exactly what must not be
    guessed. The identity lookup below is the safety this trades for.
    """
    entered = read_line("kill pid: ", theme)
    if not entered or not entered.strip().isdigit():
        return
    pid = int(entered.strip())

    known = next((p for p in snap["processes"] if p.pid == pid), None)
    ports = [item.port for item in snap["listeners"] if item.pid == pid]
    if known is None:
        identity = f"pid {pid} is not in this snapshot"
    else:
        identity = f"{known.name} — {human_bytes(known.memory)}, up {human_duration(known.age_seconds)}"
        if ports:
            identity += f", listening on {', '.join(str(p) for p in ports)}"

    refusal = actions.refuse_reason(pid)
    if refusal:
        sys.stdout.write(f"\r\033[K{theme.crit}refused: {refusal}{theme.reset}\n")
        sys.stdout.flush()
        time.sleep(2.0)
        return

    sys.stdout.write(f"\r\033[K{theme.warn}{identity}{theme.reset}\n")
    sys.stdout.flush()
    answer = read_line("type 'yes' to terminate: ", theme)
    if (answer or "").strip().lower() != "yes":
        return

    result = actions.terminate(pid, known.name if known else "", confirm=True)
    color = theme.ok if result.ok else theme.crit
    sys.stdout.write(f"\r\033[K{color}{result.pid} {result.outcome} {result.detail}{theme.reset}\n")
    sys.stdout.flush()
    time.sleep(1.5)


@contextlib.contextmanager
def cbreak_terminal():
    """Hold the terminal in cbreak for the whole live session.

    Entering cbreak only for the duration of the read loses every keystroke made
    while a frame is being gathered — that is most of the cycle — because the
    line discipline holds canonical-mode input until a newline that never comes.
    """
    if termios is None or not sys.stdin.isatty():
        yield False
        return
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def read_line(prompt: str, theme: Theme) -> str | None:
    """Read a line in cbreak mode, echoing it ourselves.

    cbreak turns off the terminal's own echo and line editing, so both have to be
    done here; without the manual echo the user types blind into what looks like
    a frozen screen. Returns None when the edit is cancelled with Esc.
    """
    buffer = ""
    while True:
        sys.stdout.write(f"\r\033[K{theme.accent}{prompt}{theme.reset}{buffer}\033[?25h")
        sys.stdout.flush()
        key = read_key(60.0)
        if key is None or key == "\x1b":  # timeout or Esc
            sys.stdout.write("\033[?25l")
            return None
        if key in ("\r", "\n"):
            sys.stdout.write("\033[?25l")
            return buffer
        if key in ("\x7f", "\b"):
            buffer = buffer[:-1]
        elif key == "\x03":
            sys.stdout.write("\033[?25l")
            return None
        elif key.isprintable():
            buffer += key


_pending: list[str] = []


def read_key(timeout: float) -> str | None:
    """Next key pressed, in order, or None on timeout.

    Reads through os.read rather than sys.stdin: the buffered text wrapper can
    block past the select() that just said a byte was waiting. Everything read is
    queued and served one key at a time — an earlier version returned only the
    last key of each read, which silently ate the '/' of a "/ then text" sequence
    because a frame takes long enough to gather that both arrive in one read.
    """
    if _pending:
        return _pending.pop(0)
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        data = os.read(sys.stdin.fileno(), 256)
    except (OSError, ValueError):
        return None
    _pending.extend(data.decode("utf-8", "ignore"))
    return _pending.pop(0) if _pending else None


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="machine-monitor",
        description="Live macOS machine dashboard — servers, CPU, memory, temperature.",
    )
    parser.add_argument("-d", "--dispatch", "--live", dest="live", action="store_true",
                        help="stay open and refresh; press r to refresh now, q to quit")
    parser.add_argument("-i", "--interval", type=float, default=None,
                        help="refresh seconds in live mode (default from config)")
    parser.add_argument("-n", "--top", type=int, default=None,
                        help="how many processes per table (default from config)")
    parser.add_argument("--json", action="store_true", help="emit one JSON snapshot and exit")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI color")
    parser.add_argument("--config", action="store_true", help="print the config file path and exit")
    parser.add_argument("--only", metavar="SECTION", action="append",
                        help=f"render only these sections ({', '.join(SECTION_ORDER)}); repeatable")
    parser.add_argument("--space", action="store_true",
                        help="report reclaimable disk space and exit (slow: walks project trees)")
    parser.add_argument("--clones", action="store_true",
                        help="report leftover code-sign clone litter and exit")
    parser.add_argument("--clean-clones", action="store_true",
                        help="remove clone litter no process holds open, and measure the real reclaim")
    parser.add_argument("--orphans", action="store_true",
                        help="list leftover automation browser sessions and exit")
    parser.add_argument("--sessions", nargs="?", const="", metavar="FAMILY",
                        help="list process-family sessions (optionally one FAMILY) and exit")
    parser.add_argument("--kill-session", action="append", type=int, metavar="ROOT_PID",
                        default=[], help="terminate one session by its root pid; repeatable")
    parser.add_argument("--history", nargs="?", const=24.0, type=float, metavar="HOURS",
                        help="show recorded trends over HOURS (default 24) and exit")
    parser.add_argument("--save", metavar="NAME",
                        help="save the current state as a named snapshot and exit")
    parser.add_argument("--diff", metavar="NAME",
                        help="compare the current state against a saved snapshot and exit")
    parser.add_argument("--snapshots", action="store_true",
                        help="list saved snapshots and exit")
    parser.add_argument("--reclaim", action="store_true",
                        help="terminate dev servers idle longer than the stale threshold")
    parser.add_argument("--kill-orphans", action="store_true",
                        help="terminate leftover automation browser sessions")
    parser.add_argument("--yes", action="store_true",
                        help="skip the typed confirmation for the terminating modes")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --reclaim or --kill-orphans, show targets and signal nothing")
    parser.add_argument("--check", action="store_true",
                        help="evaluate alert thresholds once and notify; meant for the background agent")
    parser.add_argument("--watch-install", nargs="?", const=300, type=int, metavar="SECONDS",
                        help="install the background alert agent (default every 300s)")
    parser.add_argument("--watch-uninstall", action="store_true",
                        help="remove the background alert agent")
    parser.add_argument("--watch-status", action="store_true",
                        help="report whether the background alert agent is installed")
    args = parser.parse_args()

    if args.watch_status:
        loaded, detail = watch.agent_status()
        print(f"alert agent: {detail}")
        return 0 if loaded else 1

    if args.watch_uninstall:
        ok, detail = watch.uninstall_agent()
        print(f"alert agent: {detail}")
        return 0 if ok else 1

    if args.watch_install is not None:
        ok, detail = watch.install_agent(Path(__file__).resolve(), max(60, args.watch_install))
        print(f"alert agent: {detail}")
        return 0 if ok else 1

    if args.config:
        print(config_mod.CONFIG_PATH)
        return 0

    cfg = config_mod.load()
    if args.top is not None:
        cfg["top_processes"] = max(1, args.top)
    if args.interval is not None:
        cfg["refresh_seconds"] = max(0.5, args.interval)
    if args.only:
        unknown = [name for name in args.only if name not in SECTION_ORDER]
        if unknown:
            parser.error(
                f"unknown section(s): {', '.join(unknown)}. "
                f"choose from: {', '.join(SECTION_ORDER)}"
            )
        cfg["sections"] = {name: name in args.only for name in SECTION_ORDER}

    theme_early = Theme(render.color_enabled() and not args.no_color)
    if args.space:
        canvas = Canvas(theme_early, render.term_width())
        draw_space(canvas, collect.reclaimable(cfg), cfg)
        print(canvas.render())
        return 0

    if args.clones or args.clean_clones:
        min_count = int(cfg["thresholds"].get("clone_min_count", 2))
        groups = clones_mod.find_clone_groups(min_count=min_count)
        canvas = Canvas(theme_early, render.term_width())
        draw_clones(canvas, groups)
        print(canvas.render())
        if not args.clean_clones or not groups:
            return 0

        removable = sum(len(g.removable) for g in groups)
        if not removable:
            print("\nevery clone is currently open — nothing to remove")
            return 0
        if not confirm_destructive(removable, "remove", args.yes, noun="clone(s)"):
            return 1
        return report_removals([
            clones_mod.purge(g, actions.remove_dirs, confirm=True, dry_run=args.dry_run)
            for g in groups
        ])

    monitor = Monitor(cfg)
    cfg["_ram_bytes"] = monitor.machine.ram_bytes

    if args.orphans:
        canvas = Canvas(theme_early, render.term_width())
        draw_orphans(canvas, collect.orphan_automation(sample_with_footprints(monitor)))
        print(canvas.render())
        return 0

    if args.check:
        sent = watch.run_check(monitor.snapshot(), cfg)
        for notice in sent:
            print(f"notified: {notice.title} — {notice.message}")
        if not sent:
            print("no new alert conditions")
        return 0

    if args.sessions is not None:
        found = collect.sessions(sample_with_footprints(monitor), cfg)
        if args.sessions:
            found = [s for s in found if s.family == args.sessions]
        canvas = Canvas(theme_early, render.term_width())
        draw_sessions(canvas, found, args.sessions)
        print(canvas.render())
        return 0

    if args.kill_session:
        found = {s.root_pid: s for s in collect.sessions(sample_with_footprints(monitor), cfg)}
        chosen, unknown = [], []
        for root in args.kill_session:
            (chosen.append(found[root]) if root in found else unknown.append(root))
        for root in unknown:
            print(f"no session rooted at pid {root} — run --sessions for the current list")
        if not chosen:
            return 1
        canvas = Canvas(theme_early, render.term_width())
        draw_sessions(canvas, chosen)
        print(canvas.render())
        if not confirm_destructive(sum(len(s.pids) for s in chosen), "terminate", args.yes):
            return 1
        # Only the root carries the family's argv mark; a helper's argv is
        # indistinguishable from the user's own. Every pid carries its start
        # time, which identifies the instance and so settles pid reuse.
        targets = [
            (pid, s.names.get(pid, ""), s.mark if pid == s.root_pid else "",
             s.starts.get(pid, ""))
            for s in chosen for pid in s.pids
        ]
        return report_kills(actions.terminate_all(targets, confirm=True, dry_run=args.dry_run))

    if args.kill_orphans:
        orphans = collect.orphan_automation(sample_with_footprints(monitor))
        canvas = Canvas(theme_early, render.term_width())
        draw_orphans(canvas, orphans)
        print(canvas.render())
        if not orphans:
            return 0
        if not confirm_destructive(len(orphans), "terminate", args.yes):
            return 1
        return report_kills(actions.terminate_all(
            # The profile is the argv mark: it is what matched these processes,
            # so every one of them carries it and a recycled pid cannot.
            [(o.pid, o.binary, o.profile) for o in orphans],
            confirm=True, dry_run=args.dry_run,
        ))

    if args.reclaim:
        snap = monitor.snapshot()
        hours = cfg["thresholds"].get("stale_server_hours", 24)
        aged = [
            item for item in snap["listeners"]
            if item.is_project and item.age_seconds >= hours * 3600
        ]
        stale = {item.pid: item for item in aged if not item.session_owner}
        spared = {item.pid: item for item in aged if item.session_owner}
        stacks = snap["stacks"]
        canvas = Canvas(theme_early, render.term_width())
        draw_reclaim(canvas, list(stale.values()), hours)
        draw_stacks(canvas, stacks)
        # Named, never silently filtered: a sweep that drops targets without
        # saying so reads as "nothing else was there".
        for item in sorted(spared.values(), key=lambda i: -i.age_seconds):
            canvas.wrapped(
                f"spared :{item.port} {item.name} (pid {item.pid}) — "
                f"belongs to a live {item.session_owner} session, not a dev server",
                canvas.t.dim,
            )
        print(canvas.render())
        targets = [(item.pid, item.name) for item in stale.values()]
        # The whole tree, supervisor included: killing only the child of a
        # watcher gets it respawned. lstart proves each pid is the same instance.
        by_pid = {p.pid: p for p in snap["processes"]}
        for stack in stacks:
            targets += [
                (pid, by_pid[pid].name, "", stack.starts.get(pid, ""))
                for pid in stack.pids if pid in by_pid
            ]
        if not targets:
            return 0
        if not confirm_destructive(len(targets), "terminate", args.yes):
            return 1
        return report_kills(actions.terminate_all(targets, confirm=True, dry_run=args.dry_run))

    if args.snapshots:
        saved = history_mod.list_snapshots()
        if not saved:
            print(f"no saved snapshots yet — create one with --save <name>")
            return 0
        for name, when, size in saved:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(when))
            print(f"{name:<24} {stamp}  {size / 1024:.0f}K")
        return 0

    if args.history is not None:
        canvas = Canvas(theme_early, render.term_width())
        draw_history(canvas, monitor.history, max(0.1, args.history))
        print(canvas.render())
        return 0

    if args.save:
        path = history_mod.save_snapshot(to_json(monitor.snapshot()), args.save)
        print(f"saved snapshot '{args.save}' to {path}")
        return 0

    if args.diff:
        before = history_mod.load_snapshot(args.diff)
        if before is None:
            print(f"no snapshot named '{args.diff}'. list them with --snapshots")
            return 1
        after = json.loads(to_json(monitor.snapshot()))
        canvas = Canvas(theme_early, render.term_width())
        draw_diff(canvas, args.diff, history_mod.diff_snapshots(before, after))
        print(canvas.render())
        return 0

    if args.json:
        print(to_json(monitor.snapshot()))
        return 0

    theme = Theme(render.color_enabled() and not args.no_color)
    interactive = args.live and sys.stdin.isatty()

    if not args.live:
        print(build(monitor.snapshot(), cfg, theme, "once"))
        return 0

    if not interactive:
        # Live mode was asked for but there is no keyboard; one render is the
        # honest answer rather than an unbreakable loop.
        print(build(monitor.snapshot(), cfg, theme, "once"), flush=True)
        return 0

    view = View()
    feed = SnapshotFeed(monitor, float(cfg["refresh_seconds"]))
    feed.start()
    sys.stdout.write("\033[H\033[2J\033[?25lgathering…\n")
    sys.stdout.flush()

    shown = -1
    dirty = True
    try:
        with cbreak_terminal():
            while True:
                snap, generation = feed.latest()
                if snap is not None and (generation != shown or dirty):
                    frame = build(view.apply(snap), cfg, theme, "live", view)
                    sys.stdout.write("\033[H\033[2J\033[?25l" + frame + "\n")
                    sys.stdout.flush()
                    shown, dirty = generation, False

                # Poll input far faster than data arrives; a view-only change is
                # answered from the snapshot already in hand, not by waiting for
                # the next one.
                key = read_key(0.05)
                if key is None:
                    continue
                if key in ("q", "Q", "\x03", "\x04"):
                    feed.stop()
                    sys.stdout.write("\033[H\033[2J\033[?25h")
                    sys.stdout.flush()
                    return 0
                if key in ("r", "R"):
                    feed.refresh_now()
                elif key == "k":
                    if snap is not None:
                        kill_interactive(snap, theme)
                        feed.refresh_now()
                    dirty = True
                elif key == "/":
                    entered = read_line("filter: ", theme)
                    if entered is not None:
                        view.filter = entered.strip()
                    dirty = True
                elif key in SORT_KEYS:
                    field = SORT_KEYS[key][0]
                    # Pressing the active sort key again clears it, so the same
                    # key is both the way in and the way out.
                    view.sort = "" if view.sort == field else field
                    dirty = True
                elif key in ("\x1b", "0"):
                    view.filter, view.sort = "", ""
                    dirty = True
    except KeyboardInterrupt:
        return 0
    finally:
        feed.stop()
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
