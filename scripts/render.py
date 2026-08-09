"""Terminal rendering: color gating, ANSI-aware width math, bars, tables.

Width math is separated from content because padding a string that contains
ANSI escapes with str.ljust counts the escape bytes as visible columns.
"""

from __future__ import annotations

import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Column:
    """A table column. `priority` decides what survives on a narrow terminal —
    the lowest number is dropped first."""

    key: str
    title: str
    width: int
    align: str = "left"
    priority: int = 5

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Unicode calls these "Ambiguous"; every terminal in practice draws them narrow.
_FORCE_NARROW = frozenset("█▉▊▋▌▍▎▏░▒▓●○◐■□▲▼←→↑↓✓✗⚠·")


def color_enabled() -> bool:
    """Gate on the environment, never on isatty().

    isatty() is False whenever this runs under an agent harness or a pipe,
    while the terminal on the other end renders color fine.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("MM_NO_COLOR"):
        return False
    term = os.environ.get("TERM", "")
    if term in ("", "dumb"):
        return False
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return True
    return "color" in term or term.startswith(("xterm", "screen", "tmux", "vt100", "rxvt"))


class Theme:
    """256-color SGR codes. Every attribute is '' when color is disabled."""

    def __init__(self, enabled: bool) -> None:
        def c(code: str) -> str:
            return code if enabled else ""

        self.enabled = enabled
        self.reset = c("\033[0m")
        self.bold = c("\033[1m")
        self.dim = c("\033[2m")

        self.title = c("\033[38;5;45m")
        self.section = c("\033[38;5;214m")
        self.label = c("\033[38;5;244m")
        self.value = c("\033[38;5;255m")
        self.pid = c("\033[38;5;220m")
        self.port = c("\033[38;5;119m")
        self.divider = c("\033[38;5;238m")
        self.kill = c("\033[38;5;203m")

        self.ok = c("\033[38;5;46m")
        self.good = c("\033[38;5;148m")
        self.warn = c("\033[38;5;208m")
        self.crit = c("\033[38;5;196m")
        self.info = c("\033[38;5;74m")
        self.accent = c("\033[38;5;141m")

    def level(self, value: float, warn: float, crit: float) -> str:
        """Pick a color by threshold. Used for every numeric cell."""
        if value >= crit:
            return self.crit
        if value >= warn:
            return self.warn
        return self.good

    def paint(self, text: str, color: str) -> str:
        if not color or not self.enabled:
            return text
        return f"{color}{text}{self.reset}"


@lru_cache(maxsize=4096)
def _char_width(ch: str) -> int:
    """Display columns for one character. Memoised: a full frame asks this
    ~14,000 times over an alphabet of a few dozen distinct characters."""
    if unicodedata.combining(ch):
        return 0
    if ch in _FORCE_NARROW:
        return 1
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_len(s: str) -> int:
    """Display columns the string occupies, ignoring ANSI escapes."""
    return sum(_char_width(ch) for ch in _ANSI_RE.sub("", s))


def truncate(s: str, width: int, ellipsis: str = "…") -> str:
    """Cut to `width` display columns. Assumes s carries no ANSI escapes."""
    if width <= 0:
        return ""
    if visible_len(s) <= width:
        return s
    out, used = [], 0
    budget = width - _char_width(ellipsis)
    for ch in s:
        w = _char_width(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out) + ellipsis


def pad(s: str, width: int, align: str = "left") -> str:
    """Pad to `width` display columns, counting only visible characters."""
    gap = width - visible_len(s)
    if gap <= 0:
        return s
    if align == "right":
        return " " * gap + s
    if align == "center":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def term_width(default: int = 120) -> int:
    """Terminal columns, with a usable default when there is no tty."""
    try:
        cols = shutil.get_terminal_size(fallback=(0, 0)).columns
    except Exception:
        cols = 0
    if cols <= 0:
        env = os.environ.get("COLUMNS")
        if env and env.isdigit():
            cols = int(env)
    if cols <= 0:
        cols = default
    return max(60, min(cols, 240))


class Canvas:
    """Accumulates lines, then emits them in one write."""

    def __init__(self, theme: Theme, width: int) -> None:
        self.t = theme
        self.width = width
        self.lines: list[str] = []

    def raw(self, text: str = "") -> None:
        self.lines.append(text)

    def rule(self, char: str = "─") -> None:
        self.lines.append(self.t.paint(char * self.width, self.t.divider))

    def section(self, label: str, note: str = "") -> None:
        self.lines.append("")
        head = f"{self.t.bold}{self.t.section}  ▌ {label}{self.t.reset}"
        room = self.width - visible_len(head) - 2
        if note and room > 12:
            head += f"  {self.t.dim}{truncate(note, room)}{self.t.reset}"
        self.lines.append(head)
        self.rule()

    def fields(self, chunks: list[str], indent: str = "  ", sep: str = "   ") -> None:
        """Emit pre-colored chunks, wrapping onto a new line before overflowing.

        Free-form rows (the device bar, thermal readings, footer hints) have no
        column grid to constrain them, so on a narrow terminal they are the lines
        that run past the right edge.
        """
        line, used = indent, visible_len(indent)
        for chunk in chunks:
            width = visible_len(chunk)
            addition = width if line == indent else width + len(sep)
            if used + addition > self.width and line != indent:
                self.lines.append(line)
                line, used = indent, visible_len(indent)
                addition = width
            if line != indent:
                line += sep
            line += chunk
            used += addition
        if line.strip():
            self.lines.append(line)

    def wrapped(self, text: str, color: str = "", indent: str = "  ") -> None:
        """Emit plain text, word-wrapped at the canvas width.

        Continuation lines are indented past the first line's marker so a wrapped
        alert still reads as one item rather than as a new bullet.
        """
        hanging = indent + "  "
        words, line, prefix = text.split(), "", indent
        for word in words:
            budget = max(20, self.width - visible_len(prefix))
            candidate = f"{line} {word}".strip()
            if visible_len(candidate) > budget and line:
                self.lines.append(prefix + self.t.paint(line, color))
                line, prefix = word, hanging
            else:
                line = candidate
        if line:
            self.lines.append(prefix + self.t.paint(line, color))

    def columns(self, cells: list[tuple[str, int, str]]) -> str:
        """Join (text, width, align) cells with two spaces between them."""
        return "  " + "  ".join(pad(text, w, a) for text, w, a in cells)

    def fit(self, specs: list[Column], min_flex: int = 20) -> list[Column]:
        """Size a table to the canvas exactly.

        The last column absorbs the leftover width. When even that leaves it too
        narrow, the lowest-priority columns are dropped rather than letting rows
        run past the right edge — an over-wide row wraps in the terminal and
        destroys the alignment of every row below it.
        """
        fixed = list(specs[:-1])
        flex = specs[-1]
        while True:
            # Row shape is: 2 indent + widths + 2 between each pair of columns.
            leftover = self.width - 2 - sum(col.width for col in fixed) - 2 * len(fixed)
            if leftover >= min_flex or not fixed:
                break
            victim = min(range(len(fixed)), key=lambda i: fixed[i].priority)
            fixed.pop(victim)
        return fixed + [
            Column(flex.key, flex.title, max(min_flex, leftover), flex.align, flex.priority)
        ]

    def table(self, specs: list[Column], rows: list[dict[str, tuple[str, str]]]) -> None:
        """Render a table from (plain_text, color) cells.

        Cells arrive uncolored so truncation happens on the real text; colorizing
        first and cutting afterwards can slice an escape sequence in half.
        """
        kept = self.fit(specs)
        self.header_row([(col.title, col.width, col.align) for col in kept])
        for row in rows:
            cells = []
            for col in kept:
                text, color = row.get(col.key, ("", ""))
                cells.append((self.t.paint(truncate(text, col.width), color), col.width, col.align))
            self.lines.append(self.columns(cells))

    def header_row(self, cells: list[tuple[str, int, str]]) -> None:
        painted = [(f"{self.t.bold}{self.t.label}{txt}{self.t.reset}", w, a) for txt, w, a in cells]
        self.lines.append(self.columns(painted))

    def bar(self, pct: float, width: int, color: str) -> str:
        """Block-glyph bar. Always exactly `width` cells wide."""
        pct = 0.0 if pct != pct else max(0.0, min(100.0, pct))  # NaN-safe clamp
        filled = int(round(pct / 100.0 * width))
        filled = max(0, min(width, filled))
        body = "█" * filled + "░" * (width - filled)
        return f"{color}{body}{self.t.reset}" if self.t.enabled else body

    def gauge(self, pct: float, width: int, warn: float, crit: float) -> str:
        return self.bar(pct, width, self.t.level(pct, warn, crit))

    def render(self) -> str:
        return "\n".join(self.lines)


# ── sparkline ────────────────────────────────────────────────────────────────

# Braille cells carry two columns of four rows each, so one character holds two
# data points at four vertical levels — four times the resolution of a block
# glyph in the same width.
_LEFT_BITS = (0x40, 0x04, 0x02, 0x01)   # bottom row upward
_RIGHT_BITS = (0x80, 0x20, 0x10, 0x08)


def sparkline(values: list[float], width: int = 20, ceiling: float | None = None) -> str:
    """Render values as braille. Always returns exactly `width` characters."""
    if width <= 0:
        return ""
    if not values:
        return " " * width

    wanted = width * 2
    points = _resample(values, wanted)
    top = ceiling if ceiling is not None else max(max(points), 1.0)
    top = max(top, 1e-9)

    out = []
    for index in range(0, wanted, 2):
        left = _height(points[index], top)
        right = _height(points[index + 1], top)
        bits = 0
        for level in range(left):
            bits |= _LEFT_BITS[level]
        for level in range(right):
            bits |= _RIGHT_BITS[level]
        out.append(chr(0x2800 + bits))
    return "".join(out)


def _height(value: float, top: float) -> int:
    """0 to 4 filled rows. A non-zero value never renders as empty."""
    if value <= 0:
        return 0
    return max(1, min(4, int(round(value / top * 4))))


def _resample(values: list[float], count: int) -> list[float]:
    """Fit any number of samples to exactly `count` slots.

    Short series are left-padded with zeros so a fresh database renders as a line
    growing in from the left rather than a stretched flat line that implies
    history nobody has.
    """
    if len(values) == count:
        return list(values)
    if len(values) < count:
        return [0.0] * (count - len(values)) + list(values)
    step = len(values) / count
    return [
        max(values[int(i * step): max(int(i * step) + 1, int((i + 1) * step))] or [0.0])
        for i in range(count)
    ]


def human_bytes(n: float) -> str:
    """Base-1024 sizes. Returns e.g. '1.4G', '812M', '4.0K'."""
    n = float(n)
    for unit, div in (("T", 2**40), ("G", 2**30), ("M", 2**20), ("K", 2**10)):
        if abs(n) >= div:
            val = n / div
            return f"{val:.1f}{unit}" if val < 10 else f"{val:.0f}{unit}"
    return f"{n:.0f}B"


def human_duration(seconds: float) -> str:
    """Compact age: 3d17h, 4h12m, 8m, 42s."""
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"
