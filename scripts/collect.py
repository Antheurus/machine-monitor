"""Data sources.

Rule of thumb applied throughout: shell out to the tool that owns the data, parse
it in Python. ctypes is used only for the two things no CLI on macOS exposes —
per-core CPU ticks (host_processor_info) and die temperature (IOHIDEventSystemClient).

Every collector degrades to None/empty rather than raising, so one unavailable
source never takes the dashboard down.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# ── subprocess helper ────────────────────────────────────────────────────────


def run(cmd: list[str], timeout: float = 5.0) -> str:
    """Run a command and return stdout, or '' on any failure.

    Never for lsof or du: both exit 1 on one unreadable entry while printing a
    valid answer, and this drops it. That hid every cwd (cwd_for_pids) and a
    27 GB cache (_dir_size) before each was moved off it.
    """
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def sysctl_many(keys: list[str]) -> dict[str, str]:
    """Fetch many sysctl keys in one fork. Missing keys are simply absent."""
    out = run(["sysctl"] + keys)
    values: dict[str, str] = {}
    for line in out.splitlines():
        if ": " in line:
            key, _, val = line.partition(": ")
        elif " = " in line:
            key, _, val = line.partition(" = ")
        else:
            continue
        values[key.strip()] = val.strip()
    return values


# ── machine identity ─────────────────────────────────────────────────────────


@dataclass
class Machine:
    model: str = "Mac"
    chip: str = "unknown"
    cores_total: int = 0
    cores_perf: int = 0
    cores_eff: int = 0
    ram_bytes: int = 0
    os_version: str = ""
    uptime_seconds: float = 0.0


def machine_info() -> Machine:
    keys = sysctl_many([
        "hw.model", "machdep.cpu.brand_string", "hw.physicalcpu", "hw.logicalcpu",
        "hw.perflevel0.logicalcpu", "hw.perflevel1.logicalcpu", "hw.memsize",
        "kern.boottime",
    ])
    m = Machine()
    m.model = keys.get("hw.model", "Mac")
    m.chip = keys.get("machdep.cpu.brand_string", "unknown").replace("Apple ", "")
    m.cores_total = int(keys.get("hw.logicalcpu", 0) or 0)
    m.cores_perf = int(keys.get("hw.perflevel0.logicalcpu", 0) or 0)
    m.cores_eff = int(keys.get("hw.perflevel1.logicalcpu", 0) or 0)
    m.ram_bytes = int(keys.get("hw.memsize", 0) or 0)

    boot = keys.get("kern.boottime", "")
    match = re.search(r"sec\s*=\s*(\d+)", boot)
    if match:
        m.uptime_seconds = max(0.0, time.time() - int(match.group(1)))

    ver = run(["sw_vers", "-productVersion"]).strip()
    build = run(["sw_vers", "-buildVersion"]).strip()
    m.os_version = f"{ver} ({build})" if ver else ""

    # A friendlier model name than hw.model's "Mac14,9", when it's cheap to get.
    marketing = run(["sysctl", "-n", "hw.product"]).strip()
    if marketing:
        m.model = marketing
    return m


# ── memory ───────────────────────────────────────────────────────────────────


@dataclass
class Memory:
    total: int = 0
    wired: int = 0
    active: int = 0
    inactive: int = 0
    compressed: int = 0
    free: int = 0
    used: int = 0
    swap_total: int = 0
    swap_used: int = 0
    page_size: int = 4096
    available: bool = False

    @property
    def used_pct(self) -> float:
        return 100.0 * self.used / self.total if self.total else 0.0

    @property
    def swap_pct(self) -> float:
        return 100.0 * self.swap_used / self.swap_total if self.swap_total else 0.0


def memory() -> Memory:
    """Real memory accounting.

    Page size is read from vm_stat's own header, never assumed: Apple Silicon uses
    16K pages and the previous hardcoded 4096 under-reported usage by 4x.
    'used' follows htop's definition (wired + active - purgeable + compressed),
    which is what Activity Monitor calls Memory Used; inactive is reclaimable and
    is reported separately rather than folded in.
    """
    mem = Memory()
    out = run(["vm_stat"])
    if not out:
        return mem

    header = out.splitlines()[0] if out.splitlines() else ""
    size_match = re.search(r"page size of (\d+) bytes", header)
    mem.page_size = int(size_match.group(1)) if size_match else 4096

    pages: dict[str, int] = {}
    for line in out.splitlines()[1:]:
        match = re.match(r"\s*(.+?):\s+(\d+)", line)
        if match:
            pages[match.group(1).strip().lower()] = int(match.group(2))

    if not pages:
        return mem

    p = mem.page_size
    mem.wired = pages.get("pages wired down", 0) * p
    mem.active = pages.get("pages active", 0) * p
    mem.inactive = pages.get("pages inactive", 0) * p
    mem.free = pages.get("pages free", 0) * p
    purgeable = pages.get("pages purgeable", 0) * p
    # "occupied by compressor" is the physical footprint; "stored in compressor"
    # is the larger pre-compression size and would overcount.
    mem.compressed = pages.get("pages occupied by compressor", 0) * p
    mem.used = mem.wired + max(0, mem.active - purgeable) + mem.compressed

    mem.total = int(sysctl_many(["hw.memsize"]).get("hw.memsize", 0) or 0)

    swap = run(["sysctl", "-n", "vm.swapusage"])
    swap_match = re.search(r"total\s*=\s*([\d.]+)M.*?used\s*=\s*([\d.]+)M", swap)
    if swap_match:
        mem.swap_total = int(float(swap_match.group(1)) * 2**20)
        mem.swap_used = int(float(swap_match.group(2)) * 2**20)

    mem.available = True
    return mem


# ── per-core CPU (mach host_processor_info) ──────────────────────────────────

_PROCESSOR_CPU_LOAD_INFO = 2
_CPU_STATE_MAX = 4
_CPU_STATE_IDLE = 2


class CoreSampler:
    """Per-core busy percentage from Mach CPU tick counters.

    No CLI exposes per-core ticks, so this is one of the two ctypes users here.
    Percentages are a delta between successive calls, which is what makes them
    *live* — unlike `ps aux`'s %CPU, which is an average over the process's
    entire lifetime and barely moves for a long-running server.
    """

    def __init__(self) -> None:
        self._prev: list[tuple[int, ...]] | None = None
        self._libc = None
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            libc.mach_host_self.restype = ctypes.c_uint
            libc.host_processor_info.argtypes = [
                ctypes.c_uint, ctypes.c_int,
                ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
                ctypes.POINTER(ctypes.c_uint),
            ]
            libc.host_processor_info.restype = ctypes.c_int
            self._libc = libc
            self._host = libc.mach_host_self()
        except (OSError, AttributeError):
            self._libc = None

    def _ticks(self) -> list[tuple[int, ...]] | None:
        if self._libc is None:
            return None
        count = ctypes.c_uint()
        info = ctypes.POINTER(ctypes.c_int)()
        size = ctypes.c_uint()
        rc = self._libc.host_processor_info(
            self._host, _PROCESSOR_CPU_LOAD_INFO,
            ctypes.byref(count), ctypes.byref(info), ctypes.byref(size),
        )
        if rc != 0:
            return None
        return [
            tuple(info[i * _CPU_STATE_MAX + s] for s in range(_CPU_STATE_MAX))
            for i in range(count.value)
        ]

    def sample(self) -> list[float]:
        """Busy% per core since the previous call. First call primes and returns []."""
        now = self._ticks()
        if now is None:
            return []
        prev, self._prev = self._prev, now
        if prev is None or len(prev) != len(now):
            return []
        out = []
        for before, after in zip(prev, now):
            delta = [a - b for b, a in zip(before, after)]
            total = sum(delta)
            idle = delta[_CPU_STATE_IDLE]
            out.append(100.0 * (total - idle) / total if total > 0 else 0.0)
        return out


# ── processes ────────────────────────────────────────────────────────────────


@dataclass
class Process:
    pid: int
    name: str
    rss: int
    cpu: float = 0.0
    age_seconds: float = 0.0
    cwd: str = ""
    path: str = ""
    footprint: int = 0
    ppid: int = 0
    children: int = 0
    owner: str = ""
    cpu_seconds: float = 0.0

    @property
    def lifetime_cpu_pct(self) -> float:
        """Average CPU used across the process's whole life, as a percent of one core.

        This is what `ps %CPU` reports, and `references/metrics.md` is right that
        it is the wrong answer to "what is busy now" — a dev server that pinned a
        core for an hour four days ago reads near zero. It is the right answer to
        a different question: has this process been grinding the entire time it
        has existed? A momentary spike cannot move it, so pairing it with the
        live `cpu` separates a daemon stuck in a loop from an app that is briefly
        busy.
        """
        return 100.0 * self.cpu_seconds / self.age_seconds if self.age_seconds > 0 else 0.0

    @property
    def memory(self) -> int:
        """Physical footprint when it is known, resident size otherwise.

        Footprint is what Activity Monitor shows and what actually costs the
        machine: it counts compressed pages and IOKit mappings, and does not
        double-count shared framework pages. Measured gap on this machine — a VM
        process at 838M RSS had a 4105M footprint, so ranking by RSS put it
        fourth when it was in fact the largest consumer on the system.
        """
        return self.footprint or self.rss

    @property
    def display_name(self) -> str:
        """Owner first, so a truncated column still names the app.

        `name` itself is never rewritten: actions.refuse_reason re-reads `ps
        comm=` and compares it against the caller's copy, so a decorated name
        would make every kill of a resolved helper look like pid reuse.
        """
        return f"{self.owner} · {self.name}" if self.owner else self.name


def _parse_etime(value: str) -> float:
    """ps etime: [[dd-]hh:]mm:ss"""
    days = 0
    if "-" in value:
        day_part, _, value = value.partition("-")
        days = int(day_part or 0)
    parts = [float(p) for p in value.split(":")] if value else [0]
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return days * 86400 + seconds


def _parse_cputime(value: str) -> float:
    """ps time: [[dd-]hh:]mm:ss.ss"""
    return _parse_etime(value)


class ProcessSampler:
    """Live per-process CPU% by differencing cumulative CPU time over wall time.

    This is the same arithmetic `top` performs internally, but the interval is
    ours to choose, and it covers every process rather than top's truncated list.
    `comm` is read as a whole trailing field, so an executable path containing
    spaces ("Google Chrome Helper (GPU)") keeps its real name instead of being
    cut at the first space.
    """

    _FIELDS = ["ps", "-eo", "pid=,ppid=,rss=,etime=,time=,comm="]

    def __init__(self) -> None:
        self._prev: dict[int, float] = {}
        self._prev_at: float | None = None

    @staticmethod
    def _snapshot() -> dict[int, tuple[int, float, float, str, int]]:
        out = run(ProcessSampler._FIELDS, timeout=8)
        rows: dict[int, tuple[int, float, float, str, int]] = {}
        for line in out.splitlines():
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            try:
                pid, ppid = int(parts[0]), int(parts[1])
                rss = int(parts[2]) * 1024
            except ValueError:
                continue
            rows[pid] = (
                rss, _parse_etime(parts[3]), _parse_cputime(parts[4]), parts[5].strip(), ppid,
            )
        return rows

    def sample(self, interval: float = 0.5) -> list[Process]:
        first = self._snapshot()
        t0 = time.monotonic()
        if self._prev_at is None:
            time.sleep(max(0.05, interval))
            second = self._snapshot()
            t1 = time.monotonic()
            base, base_at, current, now = first, t0, second, t1
        else:
            base, base_at, current, now = self._prev, self._prev_at, first, t0

        elapsed = max(1e-6, now - base_at)
        procs: list[Process] = []
        child_count: dict[int, int] = {}
        for _, (_, _, _, _, ppid) in current.items():
            child_count[ppid] = child_count.get(ppid, 0) + 1

        for pid, (rss, age, cpu_time, path, ppid) in current.items():
            before = base.get(pid)
            cpu = 100.0 * (cpu_time - before[2]) / elapsed if before else 0.0
            # `comm` is a full executable path; take the basename of the WHOLE
            # field rather than splitting on whitespace, so "…/MacOS/Google
            # Chrome Helper (GPU)" keeps its name instead of becoming "Google".
            name = path.rsplit("/", 1)[-1] or path
            procs.append(Process(
                pid=pid, name=name, path=path, rss=rss, ppid=ppid,
                cpu=max(0.0, cpu), age_seconds=age, cpu_seconds=cpu_time,
                children=child_count.get(pid, 0),
            ))

        self._prev = current
        self._prev_at = now
        return procs


def footprints() -> dict[int, int]:
    """Physical footprint per pid, in bytes.

    `ps` has no footprint column and the `proc_pid_rusage` syscall returns EPERM
    for any process the caller does not own (59 of 60 sampled here) — it needs a
    private codesign entitlement only Apple's own binaries carry. `top`'s MEM
    column is the footprint and `top` does carry that entitlement, so shelling
    out is not laziness, it is the only route available.
    """
    out = run(["top", "-l", "1", "-n", "9999", "-stats", "pid,mem"], timeout=12)
    if "PID" not in out:
        return {}
    result: dict[int, int] = {}
    units = {"B": 1, "K": 1024, "M": 2**20, "G": 2**30, "T": 2**40}
    for line in out.split("PID")[-1].splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        raw = parts[1].rstrip("+")  # a trailing + means "growing", not a unit
        try:
            value = float(raw[:-1]) * units[raw[-1]] if raw[-1] in units else float(raw)
        except (ValueError, IndexError):
            continue
        result[int(parts[0])] = int(value)
    return result


# Shared XPC helpers, whose executable name identifies the framework rather than
# the app that asked for it — and launchd reparents them, so ppid is dead too.
GENERIC_HELPERS = frozenset({
    "com.apple.Virtualization.VirtualMachine",
})

_APP_BUNDLE = re.compile(r"/([^/]+)\.app/")
_owner_cache: dict[tuple[int, str], str] = {}


def owner_app(pid: int, name: str = "") -> str:
    """Which app spawned a shared helper, read from the files it holds open.

    The most frequently referenced bundle wins rather than the first one seen: a
    helper incidentally reads a font or a resource out of some other app, and a
    first-match rule would report that instead. Returns '' when nothing in the
    fd table points at a bundle, which is a normal answer, not a failure.
    """
    cached = _owner_cache.get((pid, name))
    if cached is not None:
        return cached
    try:
        # lsof exits non-zero when any single fd is unreadable, so stdout is
        # taken regardless of return code rather than through run().
        proc = subprocess.run(
            ["lsof", "-p", str(pid), "-Fn"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    tally: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        if not line.startswith("n/") or line.startswith("n/System/"):
            continue
        match = _APP_BUNDLE.search(line)
        if match:
            bundle = match.group(1)
            tally[bundle] = tally.get(bundle, 0) + 1
    owner = max(tally, key=lambda k: tally[k]) if tally else ""
    _owner_cache[(pid, name)] = owner
    return owner


def resolve_owners(procs: list[Process]) -> None:
    """Attach an owning app to every generic helper in `procs`, in place."""
    for proc in procs:
        if proc.name in GENERIC_HELPERS:
            proc.owner = owner_app(proc.pid, proc.name)


# ── throughput ───────────────────────────────────────────────────────────────


@dataclass
class Rate:
    """A counter pair turned into a per-second rate."""

    in_total: int = 0
    out_total: int = 0
    in_rate: float = 0.0
    out_rate: float = 0.0
    label: str = ""
    primed: bool = False


class _CounterSampler:
    """Shared shape for anything measured as 'counter now minus counter before'.

    The first call can only prime — a rate needs two readings — so it reports
    primed=False rather than a fabricated 0.0/s that looks like a real idle
    reading.
    """

    def __init__(self) -> None:
        self._prev: dict[str, tuple[int, int]] = {}
        self._prev_at: float | None = None

    def _rates(self, current: dict[str, tuple[int, int]]) -> tuple[dict[str, Rate], bool]:
        now = time.monotonic()
        prev, prev_at = self._prev, self._prev_at
        self._prev, self._prev_at = current, now
        primed = prev_at is not None
        elapsed = max(1e-6, now - prev_at) if prev_at is not None else 1.0

        out: dict[str, Rate] = {}
        for key, (rx, tx) in current.items():
            before = prev.get(key)
            rate = Rate(in_total=rx, out_total=tx, label=key, primed=primed)
            if before:
                # Counters reset when an interface goes down; a negative delta is
                # a reset, not negative traffic.
                rate.in_rate = max(0.0, (rx - before[0]) / elapsed)
                rate.out_rate = max(0.0, (tx - before[1]) / elapsed)
            out[key] = rate
        return out, primed


class NetworkSampler(_CounterSampler):
    """Per-interface throughput from netstat's own counters.

    The kernel route-table route (`sysctl NET_RT_IFLIST2`) was tried first and
    abandoned: scanning every offset of the 180-byte en0 message found no field
    holding the true 39.5 GB lifetime total, and the plausible offsets carry only
    its low 32 bits, so any rate built on them silently breaks past 4 GB.
    """

    def sample(self) -> dict[str, Rate]:
        out = run(["netstat", "-ibn"], timeout=6)
        lines = out.splitlines()
        if not lines:
            return {}
        header = lines[0].split()
        try:
            rx_col, tx_col = header.index("Ibytes"), header.index("Obytes")
        except ValueError:
            return {}

        current: dict[str, tuple[int, int]] = {}
        for line in lines[1:]:
            fields = line.split()
            if len(fields) <= max(rx_col, tx_col) or len(fields) < 3:
                continue
            # Each interface repeats once per bound address; the <Link#N> row is
            # the one carrying the interface totals.
            if not fields[2].startswith("<Link"):
                continue
            try:
                current[fields[0]] = (int(fields[rx_col]), int(fields[tx_col]))
            except ValueError:
                continue
        rates, _ = self._rates(current)
        return rates


class DiskIOSampler(_CounterSampler):
    """Disk throughput from iostat's cumulative totals.

    `-I` asks for totals rather than a windowed average, which means no blocking
    sample interval: two cheap reads a frame apart give the rate.
    """

    def sample(self) -> dict[str, Rate]:
        out = run(["iostat", "-Id"], timeout=6)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if len(lines) < 3:
            return {}
        names = lines[0].split()
        values = lines[-1].split()
        current: dict[str, tuple[int, int]] = {}
        # Each disk contributes three columns: KB/t, xfrs, MB.
        for index, name in enumerate(names):
            base = index * 3
            if base + 2 >= len(values):
                break
            try:
                megabytes = float(values[base + 2])
            except ValueError:
                continue
            # iostat reports one combined total, so it lands in the read slot and
            # the write slot stays zero rather than being invented.
            current[name] = (int(megabytes * 2**20), 0)
        rates, _ = self._rates(current)
        return rates


# ── listening sockets ────────────────────────────────────────────────────────


@dataclass
class Listener:
    port: int
    pid: int
    proto: str
    name: str = ""
    cwd: str = ""
    age_seconds: float = 0.0
    rss: int = 0
    label: str = ""
    is_project: bool = False
    session_owner: str = ""


def listeners() -> list[Listener]:
    """Every listening TCP socket plus bound UDP sockets.

    lsof's -F field mode is parsed instead of its columnar output: the column
    layout breaks on IPv6 ("[::1]:5432") and on any address containing a colon,
    and UDP sockets never carry the "LISTEN" string the old column filter
    required, so they were invisible entirely.
    """
    found: dict[tuple[int, int, str], Listener] = {}

    for args, proto in ((["-iTCP", "-sTCP:LISTEN"], "TCP"), (["-iUDP"], "UDP")):
        out = run(["lsof", "-nP", "-F", "pn"] + args, timeout=8)
        pid = 0
        for line in out.splitlines():
            tag, value = line[:1], line[1:]
            if tag == "p":
                try:
                    pid = int(value)
                except ValueError:
                    pid = 0
            elif tag == "n" and pid:
                if "->" in value:  # an established connection, not a listener
                    continue
                match = re.search(r":(\d+)$", value)
                if not match:
                    continue
                port = int(match.group(1))
                key = (port, pid, proto)
                if key not in found:
                    found[key] = Listener(port=port, pid=pid, proto=proto)

    return sorted(found.values(), key=lambda item: (item.port, item.pid))


# An agent session started from a project directory leaves helpers that listen on
# a port, sit in that project's cwd, and outlive any sane "stale" threshold — so
# every test a dev-server sweep applies says yes. Killing one silently breaks the
# tools of a session that is still running.
DEFAULT_SESSION_OWNERS: list[str] = ["claude", "codex", "cursor", "windsurf"]


def mark_session_owned(listener_list: list[Listener], cfg: dict | None = None,
                       tree: dict[int, tuple[int, str]] | None = None) -> None:
    """Name the live agent session that owns each listener, where there is one.

    Ancestry, never the process's own argv — the same rule `sessions()` is built
    on, and for the same reason. A helper does not repeat its parent's identity:
    `plannotator` is spelled exactly like a project binary, and only its ppid
    chain reveals it is a child of a running `claude`.

    That the check is against a LIVE ancestor is what keeps it from being too
    broad. A dev server whose session has since exited reparents to launchd, so
    the chain ends at pid 1, it is correctly left claimable, and the guard costs
    a genuine leftover nothing.
    """
    owners = [n.lower() for n in (cfg or {}).get("session_owners", DEFAULT_SESSION_OWNERS)]
    if not owners or not listener_list:
        return
    tree = tree if tree is not None else _ps_tree()

    def owner_of(pid: int) -> str:
        seen = set()
        cur = tree.get(pid, (0, ""))[0]
        while cur > 1 and cur not in seen:
            seen.add(cur)
            binary = tree.get(cur, (0, ""))[1].split(" ", 1)[0].rsplit("/", 1)[-1].lower()
            for name in owners:
                if binary == name:
                    return name
            cur = tree.get(cur, (0, ""))[0]
        return ""

    resolved: dict[int, str] = {}
    for item in listener_list:
        if item.pid not in resolved:
            resolved[item.pid] = owner_of(item.pid)
        item.session_owner = resolved[item.pid]


def cwd_for_pids(pids: list[int]) -> dict[int, str]:
    """Working directory per pid, in a single lsof call.

    This is what tells two identical `next-server` processes apart — which
    worktree each one was started from.
    """
    if not pids:
        return {}
    # lsof exits 1 when any one pid is unreadable (root-owned), and run() drops
    # stdout on a non-zero exit — so one foreign pid blanked every answer.
    try:
        out = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-F", "pn", "-p", ",".join(str(p) for p in pids)],
            capture_output=True, text=True, timeout=8, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    result: dict[int, str] = {}
    pid = 0
    for line in out.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            try:
                pid = int(value)
            except ValueError:
                pid = 0
        elif tag == "n" and pid:
            result.setdefault(pid, value)
    return result


def shorten_path(path: str, roots: list[str]) -> tuple[str, bool]:
    """Trim a cwd to the part that identifies the project.

    Returns (display, is_project). is_project is True only for a directory under
    one of the configured project roots — a daemon whose cwd is "/" or the home
    directory is not a dev server, and treating it as one turns the duplicate and
    stale-server checks into noise.
    """
    if not path:
        return "", False
    home = str(Path.home())
    for root in roots:
        expanded = os.path.expanduser(root)
        if path.startswith(expanded + os.sep):
            trimmed = path[len(expanded) + 1:]
            return trimmed, "/Library/" not in path
    if path.startswith(home + os.sep):
        return "~/" + path[len(home) + 1:], False
    return path, False


# ── temperature (IOHIDEventSystemClient) ─────────────────────────────────────

_KHID_PAGE_APPLE_VENDOR = 0xFF00
_KHID_USAGE_TEMPERATURE = 5
_KIOHID_EVENT_TYPE_TEMPERATURE = 15
_CF_STRING_ENCODING_UTF8 = 0x08000100
_CF_NUMBER_SINT32 = 3


class TemperatureReader:
    """Die temperatures via the private IOHID sensor services.

    macOS exposes no public °C on Apple Silicon — powermetrics reports thermal
    pressure only, and its `smc` sampler does not exist on arm64. The HID sensor
    route is what Stats.app, btop and macmon all use, needs no sudo and no
    install, and returns real numbers.
    """

    def __init__(self) -> None:
        self.available = False
        self._error = ""
        try:
            self._cf = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
            self._iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
            self._bind()
            self._client = self._iokit.IOHIDEventSystemClientCreate(None)
            if not self._client:
                raise OSError("IOHIDEventSystemClientCreate returned NULL")
            match = self._matching(_KHID_PAGE_APPLE_VENDOR, _KHID_USAGE_TEMPERATURE)
            self._iokit.IOHIDEventSystemClientSetMatching(self._client, match)
            self.available = True
        except (OSError, AttributeError) as exc:
            self._error = str(exc)

    def _bind(self) -> None:
        cf, iokit = self._cf, self._iokit
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFNumberCreate.restype = ctypes.c_void_p
        cf.CFNumberCreate.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
        ]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        iokit.IOHIDEventSystemClientCreate.restype = ctypes.c_void_p
        iokit.IOHIDEventSystemClientCreate.argtypes = [ctypes.c_void_p]
        iokit.IOHIDEventSystemClientSetMatching.restype = ctypes.c_int
        iokit.IOHIDEventSystemClientSetMatching.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        iokit.IOHIDEventSystemClientCopyServices.restype = ctypes.c_void_p
        iokit.IOHIDEventSystemClientCopyServices.argtypes = [ctypes.c_void_p]
        iokit.IOHIDServiceClientCopyEvent.restype = ctypes.c_void_p
        iokit.IOHIDServiceClientCopyEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_int64, ctypes.c_int32, ctypes.c_int64,
        ]
        iokit.IOHIDServiceClientCopyProperty.restype = ctypes.c_void_p
        iokit.IOHIDServiceClientCopyProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        iokit.IOHIDEventGetFloatValue.restype = ctypes.c_double
        iokit.IOHIDEventGetFloatValue.argtypes = [ctypes.c_void_p, ctypes.c_int32]

    def _cfstr(self, text: str) -> ctypes.c_void_p:
        return self._cf.CFStringCreateWithCString(None, text.encode("utf-8"), _CF_STRING_ENCODING_UTF8)

    def _matching(self, page: int, usage: int) -> ctypes.c_void_p:
        cf = self._cf
        key_cb = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        val_cb = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
        keys = (ctypes.c_void_p * 2)(self._cfstr("PrimaryUsagePage"), self._cfstr("PrimaryUsage"))
        page_val, usage_val = ctypes.c_int32(page), ctypes.c_int32(usage)
        values = (ctypes.c_void_p * 2)(
            cf.CFNumberCreate(None, _CF_NUMBER_SINT32, ctypes.byref(page_val)),
            cf.CFNumberCreate(None, _CF_NUMBER_SINT32, ctypes.byref(usage_val)),
        )
        return cf.CFDictionaryCreate(None, keys, values, 2, key_cb, val_cb)

    def _name(self, service: ctypes.c_void_p) -> str:
        ref = self._iokit.IOHIDServiceClientCopyProperty(service, self._cfstr("Product"))
        if not ref:
            return ""
        buf = ctypes.create_string_buffer(256)
        self._cf.CFStringGetCString(ref, buf, 256, _CF_STRING_ENCODING_UTF8)
        self._cf.CFRelease(ref)
        return buf.value.decode("utf-8", "ignore")

    def _value(self, service: ctypes.c_void_p) -> float:
        event = self._iokit.IOHIDServiceClientCopyEvent(
            service, _KIOHID_EVENT_TYPE_TEMPERATURE, 0, 0
        )
        if not event:
            return 0.0
        val = self._iokit.IOHIDEventGetFloatValue(
            event, _KIOHID_EVENT_TYPE_TEMPERATURE << 16
        )
        self._cf.CFRelease(event)
        return val

    def read(self) -> dict[str, float]:
        """Averaged sensor groups: cpu / gpu / battery / ssd, in °C.

        `PMU tcal` is deliberately excluded — it is a calibration constant that
        never moves, and averaging it in would drag the reported die temperature
        several degrees off.
        """
        if not self.available:
            return {}
        buckets: dict[str, list[float]] = {"cpu": [], "gpu": [], "battery": [], "ssd": []}
        try:
            services = self._iokit.IOHIDEventSystemClientCopyServices(self._client)
            if not services:
                return {}
            for index in range(self._cf.CFArrayGetCount(services)):
                service = self._cf.CFArrayGetValueAtIndex(services, index)
                name = self._name(service)
                value = self._value(service)
                if not name or not (1.0 < value < 130.0):
                    continue
                lowered = name.lower()
                if "tcal" in lowered:
                    continue
                if "battery" in lowered or "gas gauge" in lowered:
                    buckets["battery"].append(value)
                elif "nand" in lowered or "ssd" in lowered:
                    buckets["ssd"].append(value)
                elif lowered.startswith("pmu tp") and lowered.endswith("g"):
                    buckets["gpu"].append(value)
                elif "tdie" in lowered or "tdev" in lowered or lowered.startswith("pmu tp"):
                    buckets["cpu"].append(value)
            self._cf.CFRelease(services)
        except (OSError, AttributeError):
            return {}
        return {
            key: round(sum(vals) / len(vals), 1)
            for key, vals in buckets.items()
            if vals
        }


# ── thermal pressure & power draw ────────────────────────────────────────────


@dataclass
class Thermal:
    pressure: str = ""
    cpu_mw: float | None = None
    gpu_mw: float | None = None
    sudo_hint: bool = False


def thermal(cfg: dict) -> Thermal:
    result = Thermal()
    if not cfg.get("power", {}).get("use_powermetrics", True):
        return result
    if not shutil.which("powermetrics"):
        return result

    can_sudo = subprocess.run(
        ["sudo", "-n", "-l", "/usr/bin/powermetrics"],
        capture_output=True, text=True, check=False,
    ).returncode == 0
    if not can_sudo and os.geteuid() != 0:
        result.sudo_hint = True
        return result

    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    timeout = float(cfg.get("power", {}).get("powermetrics_timeout", 4))
    out = run(
        prefix + ["powermetrics", "-n", "1", "-i", "200",
                  "--samplers", "cpu_power,gpu_power,thermal"],
        timeout=timeout + 2,
    )
    if not out:
        return result

    pressure = re.search(r"Current pressure level:\s*(\S+)", out)
    if pressure:
        result.pressure = pressure.group(1)
    cpu = re.search(r"^CPU Power:\s*([\d.]+)\s*mW", out, re.MULTILINE)
    if cpu:
        result.cpu_mw = float(cpu.group(1))
    gpu = re.search(r"^GPU Power:\s*([\d.]+)\s*mW", out, re.MULTILINE)
    if gpu:
        result.gpu_mw = float(gpu.group(1))
    return result


# ── disk, battery, docker ────────────────────────────────────────────────────


@dataclass
class Disk:
    total: int = 0
    free: int = 0

    @property
    def used(self) -> int:
        return self.total - self.free

    @property
    def used_pct(self) -> float:
        return 100.0 * self.used / self.total if self.total else 0.0


def disk_usage(path: str = "/") -> Disk:
    """statvfs on / reports the whole APFS container, which is what a person
    means by "how full is my disk" — `df /` reports only the sealed system
    volume and looks alarmingly small."""
    try:
        st = os.statvfs(path)
    except OSError:
        return Disk()
    return Disk(total=st.f_blocks * st.f_frsize, free=st.f_bavail * st.f_frsize)


@dataclass
class Battery:
    percent: int | None = None
    state: str = ""
    time_left: str = ""
    cycles: int | None = None
    health_pct: int | None = None


def battery() -> Battery:
    result = Battery()
    out = run(["pmset", "-g", "batt"])
    match = re.search(r"(\d+)%;\s*([^;]+);\s*([^;]*)", out)
    if match:
        result.percent = int(match.group(1))
        result.state = match.group(2).strip()
        left = match.group(3).strip()
        result.time_left = "" if "no estimate" in left else left.replace(" remaining", "")

    raw = run(["ioreg", "-rn", "AppleSmartBattery"])
    cycles = re.search(r'"CycleCount"\s*=\s*(\d+)', raw)
    if cycles:
        result.cycles = int(cycles.group(1))
    design = re.search(r'"DesignCapacity"\s*=\s*(\d+)', raw)
    nominal = re.search(r'"NominalChargeCapacity"\s*=\s*(\d+)', raw)
    if design and nominal and int(design.group(1)) > 0:
        result.health_pct = round(100 * int(nominal.group(1)) / int(design.group(1)))
    return result


@dataclass
class Container:
    name: str
    image: str
    status: str
    cpu: str = ""
    mem: str = ""


def containers() -> list[Container]:
    if not shutil.which("docker"):
        return []
    out = run(["docker", "ps", "--format", "{{json .}}"], timeout=6)
    result: list[Container] = []
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        result.append(Container(
            name=row.get("Names", ""),
            image=row.get("Image", ""),
            status=row.get("Status", ""),
        ))
    if not result:
        return result

    stats = run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=10
    )
    usage = {}
    for line in stats.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        usage[row.get("Name", "")] = (row.get("CPUPerc", ""), row.get("MemUsage", ""))
    for item in result:
        if item.name in usage:
            item.cpu, item.mem = usage[item.name]
    return result


# ── WindowServer diagnosis ───────────────────────────────────────────────────

_RECORDER_APPS = (
    "QuickTime Player", "OBS", "Screenflick", "CleanShot", "Kap", "Screenium",
    "Camtasia", "ScreenFlow", "Snagit", "zoom.us", "Loom",
)


@dataclass
class Finding:
    factor: str
    status: str
    verdict: str
    severity: str  # "cause" | "factor" | "ok"


def windowserver_diagnosis(ps_output: str) -> list[Finding]:
    """Live checks for why WindowServer is busy.

    All process matching runs against one captured `ps` snapshot rather than a
    fresh `ps | grep` per candidate app.
    """
    findings: list[Finding] = []

    displays_json = run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=12)
    refresh = ""
    display_count = 0
    try:
        data = json.loads(displays_json) if displays_json else {}
        for item in data.get("SPDisplaysDataType", []):
            for screen in item.get("spdisplays_ndrvs", []):
                display_count += 1
                res = screen.get("_spdisplays_resolution", "")
                if "@" in res and not refresh:
                    refresh = res.strip()
    except ValueError:
        pass

    hz = re.search(r"@\s*([\d.]+)\s*Hz", refresh)
    hz_value = float(hz.group(1)) if hz else 0.0
    if hz_value >= 100:
        findings.append(Finding("Refresh rate", refresh, "CAUSE: high-Hz compositing", "cause"))
    else:
        findings.append(Finding(
            "Refresh rate", refresh or "unknown",
            "reported rate is static, not the live adaptive rate", "ok",
        ))

    decoders = [ln for ln in ps_output.splitlines() if "VTDecoderXPCService" in ln]
    if decoders:
        findings.append(Finding(
            "Video decode", f"{len(decoders)} VTDecoder instance(s)",
            "CAUSE: an app or browser is decoding video", "cause",
        ))
    else:
        findings.append(Finding("Video decode", "none", "not decoding video", "ok"))

    recording = [app for app in _RECORDER_APPS if app.lower() in ps_output.lower()]
    capture = run(["pgrep", "-x", "screencaptureui"], timeout=3).strip()
    if recording or capture:
        label = ", ".join(recording) or "screen capture active"
        findings.append(Finding("Screen recording", label, "CAUSE: screen is being captured", "cause"))
    else:
        findings.append(Finding("Screen recording", "none detected", "no known recorder running", "ok"))

    transparency = run(["defaults", "read", "com.apple.universalaccess", "reduceTransparency"]).strip()
    if transparency == "1":
        findings.append(Finding("Transparency", "REDUCED", "less compositing work", "ok"))
    else:
        findings.append(Finding("Transparency", "ON", "FACTOR: blur/vibrancy layers", "factor"))

    if display_count > 1:
        findings.append(Finding("Displays", f"{display_count} active", "FACTOR: more pixels to composite", "factor"))
    else:
        findings.append(Finding("Displays", f"{max(1, display_count)} (internal)", "", "ok"))

    return findings


# ── stuck background work ────────────────────────────────────────────────────


@dataclass
class Grinder:
    pid: int
    name: str
    cpu: float             # live, this sample
    lifetime_pct: float    # average across its whole life
    age_seconds: float
    cpu_seconds: float
    cause: str = ""        # plain-English explanation when the daemon is known
    remedy: str = ""


# Background work that has no window and therefore no obvious way to notice it is
# running. Each entry is matched on the process name and answers the two questions
# a person actually has: what started this, and how do I stop it.
KNOWN_GRINDERS: dict[str, tuple[str, str]] = {
    "StorageManagementService": (
        "the System Settings > Storage pane is open and still scanning",
        "close that pane, or terminate the process — it reopens cleanly",
    ),
    "ApplicationsStorageExtension": (
        "the System Settings > Storage pane is measuring installed apps",
        "close that pane, or terminate the process — it reopens cleanly",
    ),
    "Storage": (
        "the System Settings > Storage pane itself",
        "close that pane, or terminate the process — it reopens cleanly",
    ),
    "mds_stores": ("Spotlight is indexing", "let it finish, or exclude the volume in Spotlight settings"),
    "mdworker_shared": ("Spotlight is indexing a specific file set", "let it finish; it stops on its own"),
    "photoanalysisd": ("Photos is analysing the library for faces and scenes", "let it finish, or quit Photos"),
    "backupd": ("Time Machine is running a backup", "let it finish, or skip this backup"),
    "cloudd": ("iCloud is syncing", "let it finish; check iCloud status in System Settings"),
    "bird": ("iCloud Drive is syncing files", "let it finish; check iCloud Drive status"),
    "AssetCacheManagerService": ("content caching is serving or fetching Apple assets", "disable Content Caching in Sharing settings"),
}


def grinding(procs: list[Process], cfg: dict) -> list[Grinder]:
    """Processes that have been burning CPU for hours, not just spiking now.

    Two conditions, and needing both is the whole point. Live CPU alone flags
    every compile and every video decode. Lifetime average alone flags a process
    that worked hard early and has since gone quiet. Together they describe
    something that is busy now AND has been busy the entire time it has existed,
    which is what a stuck daemon looks like and what ordinary work does not.

    Nothing else on the dashboard can surface these: a stuck daemon has no window,
    no port and little memory, so every other section renders it as unremarkable.
    """
    th = cfg.get("thresholds", {})
    live_min = th.get("grind_cpu_pct", 25.0)
    lifetime_min = th.get("grind_lifetime_pct", 15.0)
    age_min = th.get("grind_min_hours", 2.0) * 3600

    mine = {os.getpid(), os.getppid()}
    found: list[Grinder] = []
    for proc in procs:
        if proc.pid in mine or proc.age_seconds < age_min:
            continue
        if proc.cpu < live_min or proc.lifetime_cpu_pct < lifetime_min:
            continue
        cause, remedy = KNOWN_GRINDERS.get(proc.name, ("", ""))
        found.append(Grinder(
            pid=proc.pid, name=proc.name, cpu=proc.cpu,
            lifetime_pct=proc.lifetime_cpu_pct, age_seconds=proc.age_seconds,
            cpu_seconds=proc.cpu_seconds, cause=cause, remedy=remedy,
        ))
    return sorted(found, key=lambda g: -g.cpu_seconds)


# ── derived attention items ──────────────────────────────────────────────────


@dataclass
class SpaceItem:
    path: str
    bytes: int
    kind: str
    note: str = ""


def reclaimable(cfg: dict, project_depth: int = 3) -> list[SpaceItem]:
    """Find the big, safely-regenerable directories.

    Deliberately a separate mode rather than a dashboard section: `du` over a
    tree of node_modules takes seconds, which no 3-second refresh can absorb.
    Everything reported here is rebuildable from a lockfile, a re-download, or a
    rebuild — nothing user-authored is ever listed.
    """
    items: list[SpaceItem] = []

    caches = [
        ("~/Library/Caches/ms-playwright", "browser cache", "npx playwright install to restore"),
        ("~/Library/Caches/ms-playwright-go", "browser cache", ""),
        ("~/Library/Caches/Homebrew", "package cache", "brew cleanup"),
        ("~/.npm/_cacache", "package cache", "npm cache clean --force"),
        ("~/.bun/install/cache", "package cache", "bun pm cache rm"),
        ("~/Library/pnpm/store", "package cache", "pnpm store prune"),
        ("~/.cargo/registry", "package cache", ""),
        ("~/Library/Developer/Xcode/DerivedData", "build cache", "safe to delete entirely"),
        ("~/Library/Developer/CoreSimulator/Caches", "build cache", ""),
        ("~/.gradle/caches", "build cache", ""),
        ("~/Library/Caches/go-build", "build cache", "go clean -cache"),
        ("~/Library/Caches/uv", "package cache", "uv cache clean"),
    ]
    for raw, kind, note in caches:
        path = Path(raw).expanduser()
        if not path.is_dir():
            continue
        size = _dir_size(path)
        if size > 50 * 2**20:
            items.append(SpaceItem(str(path), size, kind, note))

    for root in cfg.get("project_roots", []):
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        for target in _find_dirs(base, {"node_modules", ".next", "dist", "build", "target", ".venv"}, project_depth):
            size = _dir_size(target)
            if size > 100 * 2**20:
                kind = "dependencies" if target.name == "node_modules" else "build output"
                items.append(SpaceItem(str(target), size, kind))

    if shutil.which("docker"):
        out = run(["docker", "system", "df", "--format", "{{json .}}"], timeout=15)
        for line in out.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            reclaim = str(row.get("Reclaimable", "0B")).split()[0]
            size = _parse_size(reclaim)
            if size > 50 * 2**20:
                items.append(SpaceItem(
                    f"docker {row.get('Type', '?').lower()}", size, "docker",
                    "docker system prune",
                ))

    return sorted(items, key=lambda item: item.bytes, reverse=True)


def _find_dirs(base: Path, names: set[str], depth: int) -> list[Path]:
    """Bounded directory search that does not descend into what it finds."""
    found, frontier = [], [(base, 0)]
    while frontier:
        current, level = frontier.pop()
        if level > depth:
            continue
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in entries:
            if not entry.is_dir() or entry.is_symlink():
                continue
            if entry.name in names:
                found.append(entry)  # never recurse into a hit
            elif not entry.name.startswith("."):
                frontier.append((entry, level + 1))
    return found


def _dir_size(path: Path) -> int:
    # du exits 1 on any one unreadable entry yet still prints the total; run()
    # would drop it, and a 27 GB go-build cache vanished from --space that way.
    try:
        out = subprocess.run(["du", "-sk", str(path)], capture_output=True,
                             text=True, timeout=240, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    parts = out.split()
    return int(parts[0]) * 1024 if parts and parts[0].isdigit() else 0


def _parse_size(text: str) -> int:
    match = re.match(r"([\d.]+)\s*([KMGT]?)i?B?", text.strip(), re.IGNORECASE)
    if not match:
        return 0
    units = {"": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}
    return int(float(match.group(1)) * units.get(match.group(2).upper(), 1))


@dataclass
class Orphan:
    pid: int
    age_seconds: float
    memory: int
    profile: str
    binary: str


_AUTOMATION_MARKERS = ("playwright", "puppeteer", "selenium", "chromedriver", "automation", "chromiumdev")


def _is_automation_profile(profile: str) -> bool:
    """Distinguish an automation profile from an ordinary app's own profile.

    `--user-data-dir` is not automation-specific: every Electron app passes it,
    so matching the flag alone reported Anytype and Docker Desktop as orphans —
    50 hits of which 1 was real. A throwaway profile lives in a temp directory or
    names its framework; an app's profile lives under Application Support.
    """
    lowered = profile.lower()
    if "/library/application support/" in lowered or "/library/containers/" in lowered:
        return False
    if any(marker in lowered for marker in _AUTOMATION_MARKERS):
        return True
    return lowered.startswith(("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/"))


def orphan_automation(procs: list[Process]) -> list[Orphan]:
    """Browser processes started by automation and never cleaned up.

    Matched on `--user-data-dir`, which real user browsing never carries — never
    on a product name, which would also match the human's own browser. The needle
    is assembled at runtime and this process's own pid and parent are excluded,
    because a probe that searches for a literal it also contains finds itself and
    reports a phantom.
    """
    needle = "--user-data" + "-dir"
    mine = {os.getpid(), os.getppid()}
    out = run(["ps", "-eo", "pid=,args="], timeout=8)
    by_pid = {p.pid: p for p in procs}

    found: list[Orphan] = []
    for line in out.splitlines():
        pid_text, _, args = line.strip().partition(" ")
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid in mine or needle not in args:
            continue
        match = re.search(re.escape(needle) + r"[= ]([^\s]+)", args)
        profile = match.group(1) if match else ""
        if not profile or not _is_automation_profile(profile):
            continue
        proc = by_pid.get(pid)
        found.append(Orphan(
            pid=pid,
            age_seconds=proc.age_seconds if proc else 0.0,
            memory=proc.memory if proc else 0,
            profile=profile,
            # From `ps comm=`, never argv split on a space: an executable path
            # contains spaces, and a wrong name makes refuse_reason reject its
            # own target. Empty falls the guard back to the profile mark.
            binary=proc.name if proc else "",
        ))
    return sorted(found, key=lambda o: o.age_seconds, reverse=True)


@dataclass
class Session:
    """One process family instance: a root and everything it spawned."""

    family: str
    root_pid: int
    mark: str
    detail: str
    binary: str
    pids: list[int] = field(default_factory=list)
    names: dict[int, str] = field(default_factory=dict)
    starts: dict[int, str] = field(default_factory=dict)
    age_seconds: float = 0.0
    memory: int = 0


# Families ship as data so adding one is a config entry, never a code change.
# A marker prefixed `=` must equal a whole argv token; anything else is a plain
# substring. Both forms are needed: a profile path is a fragment, while the bare
# word `mcp` as a substring also matched `Cursor Helper: mcp-process`.
DEFAULT_FAMILIES: dict[str, list[str]] = {
    "automation-browser": [
        "playwright" + "_chromiumdev", ".playwright" + "-mcp", "puppeteer" + "_dev",
        "chromedriver", "--enable-" + "automation", "cliDaemon.js",
    ],
    "mcp-server": ["mcp" + "-server", "=mcp"],
}


def _marker_hit(args: str, marker: str) -> str:
    """The literal that matched, or '' — `=tok` is whole-token, else substring."""
    if marker.startswith("="):
        token = marker[1:]
        return token if token in args.split() else ""
    return marker if marker in args else ""


def _ps_tree() -> dict[int, tuple[int, str]]:
    """pid -> (ppid, argv), from one call."""
    out = run(["ps", "-eo", "pid=,ppid=,args="], timeout=8)
    tree: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            tree[int(parts[0])] = (int(parts[1]), parts[2])
    return tree


def _identity() -> dict[int, tuple[str, str]]:
    """pid -> (lstart, executable basename), the pair the kill guard re-checks.

    `lstart` identifies a process INSTANCE, which is what settles pid reuse;
    argv cannot, because a browser helper's `--type=gpu-process` is exactly what
    the user's own Chrome helpers say. `lstart` is always 5 whitespace-separated
    tokens, so the name is everything after them — and it is basenamed as a
    whole rather than split on space, or `Google Chrome Helper (GPU)` becomes
    `Google` and every name check then refuses its own target.
    """
    out = run(["ps", "-eo", "pid=,lstart=,comm="], timeout=8)
    found: dict[int, tuple[str, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) == 7 and parts[0].isdigit():
            found[int(parts[0])] = (" ".join(parts[1:6]), parts[6].rsplit("/", 1)[-1])
    return found


def sessions(procs: list[Process], cfg: dict | None = None) -> list[Session]:
    """Group processes into families by ANCESTRY, not by argv per process.

    Matching each process on its own argv is what a first attempt does, and it
    silently mislabels the majority: a browser's helper processes do not repeat
    `--user-data-dir`, so 8 of 40 automation processes here classified as the
    human's own Chrome. A root is matched on argv; everything beneath it is
    claimed regardless of what its own argv says. A root that turns out to sit
    under another root is absorbed, which is what folds a Playwright daemon and
    the browser it launched into one session instead of two.
    """
    families = dict(DEFAULT_FAMILIES)
    families.update((cfg or {}).get("process_families", {}))
    tree = _ps_tree()
    identity = _identity()
    mine = {os.getpid(), os.getppid()}
    by_pid = {p.pid: p for p in procs}

    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in tree.items():
        children.setdefault(ppid, []).append(pid)

    roots: dict[int, tuple[str, str]] = {}
    for pid, (_, args) in tree.items():
        if pid in mine:
            continue
        for family, markers in families.items():
            hit = next((h for h in (_marker_hit(args, m) for m in markers) if h), "")
            if not hit:
                continue
            if family == "automation-browser" and not _root_is_automation(args, hit):
                continue
            roots[pid] = (family, hit)
            break

    def has_matched_ancestor(pid: int) -> bool:
        cur = tree.get(pid, (0, ""))[0]
        while cur > 1:
            if cur in roots:
                return True
            cur = tree.get(cur, (0, ""))[0]
        return False

    found: list[Session] = []
    for pid, (family, mark) in roots.items():
        if has_matched_ancestor(pid):
            continue
        claimed, stack = [], [pid]
        while stack:
            cur = stack.pop()
            claimed.append(cur)
            stack.extend(children.get(cur, []))
        known = [by_pid[p] for p in claimed if p in by_pid]
        args = tree[pid][1]
        found.append(Session(
            family=family,
            root_pid=pid,
            mark=mark,
            detail=_session_detail(args, mark),
            binary=args.split(" ", 1)[0].rsplit("/", 1)[-1],
            pids=sorted(claimed),
            # Per pid, not the root's: a session spans several executables.
            names={p: identity[p][1] for p in claimed if p in identity},
            starts={p: identity[p][0] for p in claimed if p in identity},
            age_seconds=max((p.age_seconds for p in known), default=0.0),
            memory=sum(p.memory for p in known),
        ))
    return sorted(found, key=lambda s: s.age_seconds, reverse=True)


def _root_is_automation(args: str, hit: str) -> bool:
    """Keep the profile test that stops Electron apps reading as automation."""
    if hit != "--enable" + "-automation":
        return True
    match = re.search(r"--user-data" + r"-dir[= ]([^\s]+)", args)
    return bool(match) and _is_automation_profile(match.group(1))


def _session_detail(args: str, mark: str) -> str:
    """What a human would recognise this session by.

    A profile path when there is one, otherwise the argv token that OWNS the
    mark — `node …/gitnexus mcp` reads as `gitnexus mcp`, where the mark alone
    would just say `mcp` across sixteen indistinguishable rows.
    """
    match = re.search(r"--user-data" + r"-dir[= ]([^\s]+)", args)
    if match:
        return match.group(1)
    tokens = args.split()
    if mark in tokens:
        index = tokens.index(mark)
        owner = tokens[index - 1].rsplit("/", 1)[-1] if index else ""
        return f"{owner} {mark}".strip()
    tail = args.split(mark, 1)[1].strip().split(" ", 1)[0] if mark in args else ""
    return f"{mark} {tail}".strip()


DEFAULT_DEV_RUNNERS: list[str] = [
    "just", "make", "pnpm", "npm", "yarn", "bun", "bunx", "npx", "node", "deno",
    "tsx", "nodemon", "air", "uvicorn",
]


@dataclass
class DetachedStack:
    pid: int
    name: str
    args: str
    cwd: str
    age_seconds: float
    memory: int
    pids: list[int]
    starts: dict[int, str]


def detached_stacks(procs: list[Process], cfg: dict, listener_pids: set[int],
                    tree: dict[int, tuple[int, str]] | None = None,
                    cwds: dict[int, str] | None = None,
                    starts: dict[int, str] | None = None) -> list[DetachedStack]:
    """Dev process trees whose session is gone and which hold no port.

    A second `just dev` that loses the port race keeps its whole watch tree
    alive anyway, and SERVERS RUNNING, the stale-server alert and --reclaim are
    all built from listeners, so it is invisible to every one of them. Measured
    2026-09-11: six such copies, 4.5 GB, 7-9 days old, zero sockets.

    The root must be adopted by launchd (its launching shell or agent exited),
    must be a dev runner by executable basename, must sit under a project root,
    and must be older than the stale threshold. Memory is the whole tree's.
    """
    th = cfg.get("thresholds", {})
    min_age = th.get("stale_server_hours", 24) * 3600
    runners = {n.lower() for n in cfg.get("dev_runners", DEFAULT_DEV_RUNNERS)}
    tree = tree if tree is not None else _ps_tree()
    by_pid = {p.pid: p for p in procs}

    candidates = []
    for pid, (ppid, _) in tree.items():
        if ppid != 1:
            continue
        # The name from `comm`, never argv split on a space: executable paths
        # contain spaces, and "Google Chrome" would become "google".
        proc = by_pid.get(pid)
        if proc and proc.name.lower() in runners and proc.age_seconds >= min_age:
            candidates.append(pid)
    if not candidates:
        return []

    cwds = cwds if cwds is not None else cwd_for_pids(candidates)
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in tree.items():
        children.setdefault(ppid, []).append(pid)

    found: list[DetachedStack] = []
    for root in candidates:
        cwd, is_project = shorten_path(cwds.get(root, ""), cfg.get("project_roots", []))
        if not is_project:
            continue
        members, stack = [root], [root]
        while stack:
            for child in children.get(stack.pop(), []):
                members.append(child)
                stack.append(child)
        if listener_pids.intersection(members):
            continue
        if starts is None:
            starts = _lstarts()
        found.append(DetachedStack(
            pid=root, name=by_pid[root].name, args=tree[root][1], cwd=cwd,
            age_seconds=by_pid[root].age_seconds,
            memory=sum(by_pid[m].memory for m in members if m in by_pid),
            pids=members, starts={m: starts.get(m, "") for m in members},
        ))
    return sorted(found, key=lambda s: -s.memory)


def _lstarts() -> dict[int, str]:
    """pid -> lstart, so a kill can prove it is signalling the same instance."""
    out = run(["ps", "-eo", "pid=,lstart="], timeout=8)
    result: dict[int, str] = {}
    for line in out.splitlines():
        head, _, rest = line.strip().partition(" ")
        if head.isdigit():
            result[int(head)] = " ".join(rest.split())
    return result


def leftover_browsers(procs: list[Process], cfg: dict,
                      found: list[Session] | None = None) -> list[Session]:
    """Automation browser sessions whose launching agent is gone.

    Adopted by launchd and past the stale threshold. Beyond the memory, they
    hijack the app: macOS sees "Google Chrome" already running, so clicking the
    Dock icon with the user's own Chrome closed activates a windowless automation
    instance instead of launching. Measured 2026-09-30: three playwright-cli
    daemons, 2.5-3.5 days old, and "Chrome won't open" was the whole symptom.
    """
    min_age = cfg.get("thresholds", {}).get("stale_server_hours", 24) * 3600
    by_pid = {p.pid: p for p in procs}
    found = found if found is not None else sessions(procs, cfg)
    return [
        s for s in found
        if s.family == "automation-browser" and s.root_pid in by_pid
        and by_pid[s.root_pid].ppid == 1 and s.age_seconds >= min_age
    ]


def agent_sessions(procs: list[Process], cfg: dict) -> tuple[int, int]:
    """(count, memory) of live agent CLI sessions, each with its descendants.

    Only the outermost owner counts as a session, so an agent that spawns a
    sub-agent of the same CLI is one session. Its MCP servers and helpers are
    included: they are what closing the tab gives back. Measured 2026-08-16:
    12 open Claude sessions at 5.28 GB, the largest single consumer on 16 GB.
    """
    owners = {n.lower() for n in cfg.get("session_owners", DEFAULT_SESSION_OWNERS)}
    by_pid = {p.pid: p for p in procs}
    is_owner = {p.pid for p in procs if p.name.lower() in owners}

    def top_owner(pid: int) -> int:
        top, cur, seen = 0, pid, set()
        while cur > 1 and cur not in seen:
            seen.add(cur)
            if cur in is_owner:
                top = cur
            cur = by_pid[cur].ppid if cur in by_pid else 0
        return top

    totals: dict[int, int] = {}
    for proc in procs:
        root = top_owner(proc.pid)
        if root:
            totals[root] = totals.get(root, 0) + proc.memory
    return len(totals), sum(totals.values())


@dataclass
class Alert:
    severity: str  # "crit" | "warn" | "info"
    text: str


def attention(
    mem: Memory,
    disk: Disk,
    listener_list: list[Listener],
    procs: list[Process],
    cfg: dict,
    stacks: list[DetachedStack] | None = None,
    browsers: list[Session] | None = None,
) -> list[Alert]:
    """The judgement layer: what a person should actually act on."""
    th = cfg.get("thresholds", {})
    alerts: list[Alert] = []

    for grind in grinding(procs, cfg):
        detail = (
            f"{grind.name} has used {grind.cpu_seconds / 60:.0f} min of CPU over "
            f"{grind.age_seconds / 3600:.1f}h ({grind.lifetime_pct:.0f}% sustained, "
            f"{grind.cpu:.0f}% now)"
        )
        if grind.cause:
            alerts.append(Alert("warn", f"{detail} — {grind.cause}; {grind.remedy}"))
        else:
            alerts.append(Alert("warn", (
                f"{detail} — stuck background work, nothing on screen will show it "
                f"(pid {grind.pid})"
            )))

    if mem.available and mem.swap_pct >= th.get("swap_crit", 60):
        alerts.append(Alert("crit", (
            f"Swap {mem.swap_pct:.0f}% full ({_g(mem.swap_used)} of {_g(mem.swap_total)}) — "
            "the machine is paging to disk, everything will feel slow"
        )))
    elif mem.available and mem.swap_pct >= th.get("swap_warn", 25):
        alerts.append(Alert("warn", f"Swap {mem.swap_pct:.0f}% in use ({_g(mem.swap_used)})"))

    if mem.available and mem.used_pct >= th.get("mem_crit", 85):
        alerts.append(Alert("crit", f"Memory {mem.used_pct:.0f}% used — {_g(mem.compressed)} of it is compressed"))

    if disk.total and disk.used_pct >= th.get("disk_crit", 92):
        alerts.append(Alert("crit", f"Disk {disk.used_pct:.0f}% full — only {_g(disk.free)} free"))
    elif disk.total and disk.used_pct >= th.get("disk_warn", 80):
        alerts.append(Alert("warn", f"Disk {disk.used_pct:.0f}% full ({_g(disk.free)} free)"))

    # Only sockets whose cwd sits under a project root count as dev servers.
    # A daemon rooted at "/" or in ~/Library is not one, and counting those turned
    # both checks below into noise (Anytype alone contributed eight "servers").
    # A helper belonging to a live agent session passes both of those tests and is
    # still not a dev server, so it is excluded here as well as at the kill site.
    dev = [item for item in listener_list if item.is_project and not item.session_owner]

    # Two distinct processes serving from the same project directory is almost
    # always a leftover from an earlier session nobody shut down.
    by_project: dict[str, list[Listener]] = {}
    for item in dev:
        by_project.setdefault(item.cwd, []).append(item)
    for project, group in sorted(by_project.items()):
        pids = sorted({item.pid for item in group})
        if len(pids) > 1:
            ports = ", ".join(
                str(port) for port in sorted({item.port for item in group})
            )
            alerts.append(Alert("warn", (
                f"{len(pids)} separate processes serving {project} "
                f"(ports {ports}) — one is probably stale"
            )))

    stale_hours = th.get("stale_server_hours", 24)
    stale = [item for item in dev if item.age_seconds >= stale_hours * 3600]
    if stale:
        oldest = max(stale, key=lambda item: item.age_seconds)
        by_pid = {item.pid: item for item in stale}
        total_rss = sum(item.rss for item in by_pid.values())
        alerts.append(Alert("info", (
            f"{len(by_pid)} dev server(s) up longer than {stale_hours}h holding "
            f"{_g(total_rss)} — oldest is port {oldest.port} "
            f"({oldest.age_seconds / 86400:.1f}d, {oldest.cwd})"
        )))

    if stacks:
        biggest = stacks[0]
        alerts.append(Alert("warn", (
            f"{len(stacks)} detached dev stack(s) with no port holding "
            f"{_g(sum(s.memory for s in stacks))} — invisible to SERVERS RUNNING; "
            f"largest is pid {biggest.pid} {biggest.name} "
            f"({biggest.age_seconds / 86400:.1f}d, {biggest.cwd}). --reclaim removes them"
        )))

    if browsers:
        alerts.append(Alert("warn", (
            f"{len(browsers)} automation browser session(s) outlived their agent, holding "
            f"{_g(sum(b.memory for b in browsers))} — while your own Chrome is closed, its Dock "
            "icon activates one of these and no window opens. machine-monitor --sessions "
            "automation-browser, then --kill-session <root>"
        )))

    count, held = agent_sessions(procs, cfg)
    if count >= th.get("agent_sessions_warn", 6):
        alerts.append(Alert("info", (
            f"{count} agent sessions open holding {_g(held)} with their MCP servers — "
            "closing idle ones is usually the biggest single RAM lever"
        )))

    return alerts


def _g(value: int) -> str:
    if value < 2**30:
        return f"{value / 2**20:.0f}M"
    return f"{value / 2**30:.1f}G"
