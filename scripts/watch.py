"""Background alerting.

launchd owns the schedule and each run is a single check that exits — rather than
a long-lived daemon of our own. A resident process is one more thing that can
wedge, leak, or outlive its config; a one-shot invocation cannot.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

LABEL = "com.machine-monitor.watch"
AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
AGENT_PATH = AGENT_DIR / f"{LABEL}.plist"

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "machine-monitor"
ALERT_STATE = STATE_DIR / "alerts.json"
WATCH_LOG = STATE_DIR / "watch.log"

# How long the same condition stays quiet after being announced. Without this a
# machine sitting at 95% swap would notify on every single run, and a notifier
# that cries every five minutes gets muted — which loses the alerts that matter.
COOLDOWN_SECONDS = 3600


@dataclass
class Notice:
    key: str
    title: str
    message: str


def evaluate(snap: dict, cfg: dict) -> list[Notice]:
    """Turn a snapshot into the notices worth interrupting someone for.

    Deliberately stricter than the dashboard's own NEEDS ATTENTION section: that
    one is read on request, this one interrupts. Only crossing a critical
    threshold earns a notification.
    """
    th = cfg["thresholds"]
    notices: list[Notice] = []
    mem = snap["memory"]
    disk = snap["disk"]

    if mem.available and mem.swap_total and mem.swap_pct >= th.get("swap_crit", 60):
        notices.append(Notice(
            "swap", "Memory pressure",
            f"Swap {mem.swap_pct:.0f}% full ({mem.swap_used / 2**30:.1f}G). "
            "The machine is paging to disk.",
        ))
    if mem.available and mem.used_pct >= th.get("mem_crit", 85):
        notices.append(Notice(
            "memory", "Memory nearly full",
            f"{mem.used_pct:.0f}% used, {mem.compressed / 2**30:.1f}G of it compressed.",
        ))
    if disk.total and disk.used_pct >= th.get("disk_crit", 92):
        notices.append(Notice(
            "disk", "Disk nearly full",
            f"{disk.used_pct:.0f}% used, only {disk.free / 2**30:.1f}G free. "
            "Run: machine-monitor --space",
        ))
    temp = snap.get("temperatures", {}).get("cpu")
    if temp and temp >= th.get("temp_crit", 90):
        notices.append(Notice("temp", "Running hot", f"CPU die at {temp:.0f}°C."))

    pressure = getattr(snap.get("thermal"), "pressure", "")
    if pressure in ("Heavy", "Critical"):
        notices.append(Notice(
            "pressure", "Thermal throttling", f"Thermal pressure is {pressure}.",
        ))
    return notices


def _load_state() -> dict:
    try:
        return json.loads(ALERT_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ALERT_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def notify(notice: Notice) -> bool:
    """Raise a macOS notification. Returns whether it was delivered."""
    script = (
        f'display notification {json.dumps(notice.message)} '
        f'with title {json.dumps("machine-monitor: " + notice.title)}'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def run_check(snap: dict, cfg: dict, force: bool = False) -> list[Notice]:
    """Evaluate, notify what is not in cooldown, and record what was sent."""
    state = _load_state()
    now = time.time()
    sent: list[Notice] = []

    fresh = evaluate(snap, cfg)
    live_keys = {notice.key for notice in fresh}
    for notice in fresh:
        last = state.get(notice.key, 0)
        if not force and now - last < COOLDOWN_SECONDS:
            continue
        if notify(notice):
            state[notice.key] = now
            sent.append(notice)

    # Forget a condition once it clears, so its next occurrence alerts at once
    # instead of waiting out a cooldown that started an hour ago.
    for key in list(state):
        if key not in live_keys:
            del state[key]

    _save_state(state)
    _log(fresh, sent)
    return sent


def _log(evaluated: list[Notice], sent: list[Notice]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        summary = ", ".join(n.key for n in evaluated) or "clear"
        with WATCH_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}  conditions: {summary}  notified: {len(sent)}\n")
    except OSError:
        pass


# ── launchd agent ────────────────────────────────────────────────────────────


def agent_plist(script: Path, interval: int) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(script), "--check"],
        "StartInterval": interval,
        "RunAtLoad": False,
        "StandardOutPath": str(STATE_DIR / "watch.out"),
        "StandardErrorPath": str(STATE_DIR / "watch.err"),
    }


def install_agent(script: Path, interval: int) -> tuple[bool, str]:
    try:
        AGENT_DIR.mkdir(parents=True, exist_ok=True)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with AGENT_PATH.open("wb") as handle:
            plistlib.dump(agent_plist(script, interval), handle)
    except OSError as exc:
        return False, f"could not write {AGENT_PATH}: {exc}"

    # bootout first so reinstalling picks up a changed interval; a missing agent
    # makes it fail, which is fine and is why the result is ignored.
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                   capture_output=True, check=False)
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(AGENT_PATH)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return False, f"launchctl bootstrap failed: {result.stderr.strip()}"
    return True, f"installed, checking every {interval}s"


def uninstall_agent() -> tuple[bool, str]:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                   capture_output=True, check=False)
    if AGENT_PATH.exists():
        try:
            AGENT_PATH.unlink()
        except OSError as exc:
            return False, f"could not remove {AGENT_PATH}: {exc}"
    return True, "removed"


def agent_status() -> tuple[bool, str]:
    if not AGENT_PATH.exists():
        return False, "not installed"
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return False, "plist present but not loaded"
    interval = ""
    for line in result.stdout.splitlines():
        if "run interval" in line.lower() or "StartInterval" in line:
            interval = line.strip()
    return True, f"loaded{' — ' + interval if interval else ''}"
