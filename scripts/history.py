"""Trends over time: a SQLite ring buffer, sparklines, and saved snapshots.

Everything else in this tool answers "what is happening now". This module is the
only part that can answer "since when", which is the question that actually
identifies a leak — a process at 2 GB is unremarkable until you know it was at
200 MB this morning.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "machine-monitor"
DB_PATH = STATE_DIR / "history.db"
SNAPSHOT_DIR = STATE_DIR / "snapshots"

# Sampled metrics, in the order they are stored. Adding one here is the only
# change needed — the schema is created from this list.
METRICS = ("cpu", "mem", "swap", "disk", "temp_cpu", "net_in", "net_out", "load")

RETENTION_DAYS = 14


@dataclass
class Trend:
    metric: str
    current: float
    oldest: float
    minimum: float
    maximum: float
    samples: int
    hours: float

    @property
    def change(self) -> float:
        return self.current - self.oldest


class History:
    """Append-only samples with age-based pruning.

    Unavailable for any reason — read-only home, corrupt file, sqlite missing —
    degrades to a no-op rather than taking the dashboard down with it: history is
    a nice-to-have and must never be able to break the live view.
    """

    def __init__(self, path: Path = DB_PATH) -> None:
        self.available = False
        self.error = ""
        self._db: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # The live loop collects on a background thread, so the connection
            # must not be bound to its creating thread; the lock keeps writes serial.
            self._lock = threading.Lock()
            self._db = sqlite3.connect(path, timeout=2.0, check_same_thread=False)
            columns = ", ".join(f"{name} REAL" for name in METRICS)
            self._db.execute(
                f"CREATE TABLE IF NOT EXISTS samples (at REAL PRIMARY KEY, {columns})"
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS samples_at ON samples(at)")
            self._db.commit()
            self.available = True
        except (sqlite3.Error, OSError) as exc:
            self.error = str(exc)
            self._db = None

    def record(self, values: dict[str, float | None]) -> None:
        """Store one sample. A metric absent from `values` is stored as NULL.

        Never coerce a missing metric to 0.0: with the thermal section disabled
        that wrote a real-looking 0 °C into the series, and the trend then read
        "CPU temp 0 °C, down from 40" — a fabricated reading indistinguishable
        from a genuinely cold machine.
        """
        if not self._db:
            return
        row = [time.time()] + [
            None if values.get(name) is None else float(values[name]) for name in METRICS
        ]
        placeholders = ", ".join("?" * (len(METRICS) + 1))
        try:
            with self._lock:
                self._db.execute(
                    f"INSERT OR REPLACE INTO samples VALUES ({placeholders})", row
                )
                self._db.execute(
                    "DELETE FROM samples WHERE at < ?",
                    (time.time() - RETENTION_DAYS * 86400,),
                )
                self._db.commit()
        except sqlite3.Error as exc:
            self.error = str(exc)

    def series(self, metric: str, hours: float = 1.0, points: int = 60) -> list[float]:
        """Most recent values for one metric, oldest first."""
        if not self._db or metric not in METRICS:
            return []
        try:
            rows = self._db.execute(
                f"SELECT {metric} FROM samples "
                f"WHERE at >= ? AND {metric} IS NOT NULL ORDER BY at DESC LIMIT ?",
                (time.time() - hours * 3600, points),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [row[0] for row in reversed(rows)]

    def trend(self, metric: str, hours: float = 24.0) -> Trend | None:
        if not self._db or metric not in METRICS:
            return None
        try:
            # COUNT(metric), not COUNT(*): rows where this metric was not
            # collected must not inflate the sample count or the time span.
            row = self._db.execute(
                f"SELECT COUNT({metric}), MIN({metric}), MAX({metric}), MIN(at), MAX(at) "
                f"FROM samples WHERE at >= ? AND {metric} IS NOT NULL",
                (time.time() - hours * 3600,),
            ).fetchone()
            if not row or not row[0]:
                return None
            first = self._db.execute(
                f"SELECT {metric} FROM samples "
                f"WHERE at >= ? AND {metric} IS NOT NULL ORDER BY at ASC LIMIT 1",
                (time.time() - hours * 3600,),
            ).fetchone()
            last = self._db.execute(
                f"SELECT {metric} FROM samples WHERE {metric} IS NOT NULL ORDER BY at DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return None
        span = (row[4] - row[3]) / 3600 if row[4] and row[3] else 0.0
        return Trend(
            metric=metric, current=last[0] if last else 0.0,
            oldest=first[0] if first else 0.0,
            minimum=row[1] or 0.0, maximum=row[2] or 0.0,
            samples=row[0], hours=span,
        )

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None


# ── saved snapshots ──────────────────────────────────────────────────────────


def save_snapshot(payload: str, name: str) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{name}.json"
    path.write_text(payload, encoding="utf-8")
    return path


def load_snapshot(name: str) -> dict | None:
    path = SNAPSHOT_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_snapshots() -> list[tuple[str, float, int]]:
    if not SNAPSHOT_DIR.is_dir():
        return []
    out = []
    for path in sorted(SNAPSHOT_DIR.glob("*.json")):
        stat = path.stat()
        out.append((path.stem, stat.st_mtime, stat.st_size))
    return out


@dataclass
class Change:
    label: str
    before: str
    after: str
    direction: str  # "up" | "down" | "gone" | "new"


def diff_snapshots(before: dict, after: dict) -> list[Change]:
    """What moved between two saved snapshots.

    Reports processes and listeners by identity rather than by rank: a process
    dropping from 3rd to 5th place is noise, a process appearing or doubling its
    memory is not.
    """
    changes: list[Change] = []

    def gauge(label: str, path: tuple[str, ...], suffix: str = "%", threshold: float = 2.0) -> None:
        old, new = before, after
        for key in path:
            old = old.get(key, {}) if isinstance(old, dict) else {}
            new = new.get(key, {}) if isinstance(new, dict) else {}
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            return
        if abs(new - old) >= threshold:
            changes.append(Change(
                label, f"{old:.0f}{suffix}", f"{new:.0f}{suffix}",
                "up" if new > old else "down",
            ))

    for label, path in (
        ("memory used", ("memory", "used")),
        ("swap used", ("memory", "swap_used")),
    ):
        old = _dig(before, path)
        new = _dig(after, path)
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            delta = new - old
            if abs(delta) > 256 * 2**20:
                changes.append(Change(
                    label, f"{old / 2**30:.1f}G", f"{new / 2**30:.1f}G",
                    "up" if delta > 0 else "down",
                ))

    gauge("disk", ("disk", "used_pct"))

    old_ports = {item["port"]: item for item in before.get("listeners", [])}
    new_ports = {item["port"]: item for item in after.get("listeners", [])}
    for port in sorted(set(new_ports) - set(old_ports)):
        item = new_ports[port]
        changes.append(Change(f"port {port}", "-", item.get("name", "?"), "new"))
    for port in sorted(set(old_ports) - set(new_ports)):
        item = old_ports[port]
        changes.append(Change(f"port {port}", item.get("name", "?"), "-", "gone"))

    old_procs = {p["pid"]: p for p in before.get("top_ram", [])}
    new_procs = {p["pid"]: p for p in after.get("top_ram", [])}
    for pid, proc in new_procs.items():
        old = old_procs.get(pid)
        if not old:
            continue
        before_mem = old.get("footprint") or old.get("rss", 0)
        after_mem = proc.get("footprint") or proc.get("rss", 0)
        if before_mem and after_mem - before_mem > 256 * 2**20:
            changes.append(Change(
                f"{proc.get('name', '?')} ({pid})",
                f"{before_mem / 2**30:.1f}G", f"{after_mem / 2**30:.1f}G", "up",
            ))
    return changes


def _dig(source: dict, path: tuple[str, ...]):
    current = source
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
