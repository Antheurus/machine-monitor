# Changelog

## v2.0.0 — Real temperatures, honest memory, a full Python rewrite, and a lot more

### New in this release

**It can now tell you what to delete.** `machine-monitor --space` lists everything reclaimable —
package caches, build output, `node_modules`, Docker leftovers — with the command to clear each one.
It found 57 GB on the development machine.

**It finds browsers that automation left behind.** `machine-monitor --orphans` groups stuck
Playwright/Puppeteer sessions by profile. Six sessions, 33 processes and 940 MB were sitting on the
development machine, the oldest for over two days.

**It can clean up, not just report.** `--reclaim` terminates dev servers you forgot about,
`--kill-orphans` clears stuck automation browsers, and pressing `k` in live mode kills a process by
PID. All three make you type the word "yes" first, tell you exactly what will die, and refuse to run
without a terminal. `--dry-run` shows the list and touches nothing.

**It remembers.** Every run records a sample, so `--history` answers "when did this start" with
sparkline graphs rather than a single instant. `--save <name>` and `--diff <name>` compare the
machine against an earlier state — useful for "what changed since this morning".

**It can watch in the background.** `--watch-install` registers a macOS background check that
notifies you when swap, memory, disk or temperature crosses a critical line, at most once an hour per
condition. `--watch-uninstall` removes it. It is off until you install it.

**In live mode:** press `/` to filter by port, name or project, `c`/`m`/`a` to sort by CPU, memory or
age, `k` to kill, `q` to quit.

**Also new:** network and disk throughput, `--only <section>` to render just one part, and
`--json` now carries every added figure.

### The memory column was measuring the wrong thing

The RAM table ranked processes by resident size, which cannot see a process macOS has compressed.
Three real examples from the development machine, measured at the same moment:

- `node` — resident 3 MB, actually using **2.2 GB**
- `next-server` — resident 9 MB, actually using **2.0 GB**
- `Maccy` — resident 30 MB, actually using **2.0 GB**

The table now shows the real figure (the same one Activity Monitor shows) and keeps resident size
beside it, highlighted when the two disagree — because that gap is exactly the sign that a process
has been squeezed out to disk.

### Settings moved into a file you can edit and share

Defaults now live in `scripts/config.ini` inside the repo, so anyone can change them and send the
change back. Your own settings go in `~/.config/machine-monitor/config.ini` and only need to contain
what you want different — everything else follows the repo, so updates actually reach you. The
previous defaults carried one machine's private port numbering; that is gone.

---

### Original rewrite notes

The tool is now Python instead of bash. **The command changed** — it is `machine-monitor` (or
`python3 scripts/main.py`), not `bash scripts/monitor.sh`, and the old file is gone. That is the one
breaking change in this release; there is still nothing to install.

**It now shows real temperature.** Previous versions said Apple Silicon could not report CPU
temperature and deliberately printed none. That turned out to be wrong, and the dashboard now shows
actual CPU die, GPU, battery and SSD temperature in °C — with no password and no extra software.

**The memory number was wrong, and it mattered.** The old header could report 1.5 GB used on a
machine that was really using 12.3 GB of its 16 GB with swap 88% full — it used the wrong page size
for Apple Silicon and ignored compressed memory entirely. Memory is now correct, and swap has its own
bar, so a machine that is quietly thrashing looks like one.

**The CPU column now shows what a process is doing right now.** It used to show an average over the
process's entire life, which meant a dev server hammering a core for the last minute could look
idle after running for four days.

New in this release:

- **NEEDS ATTENTION** — tells you what to act on: swap thrashing, disk filling up, two servers
  running from the same project folder, servers left up for days.
- **Which project each server belongs to** — two identical `next-server` processes are no longer
  indistinguishable; each row shows the folder it was started from.
- **Docker containers** with live CPU and memory.
- **Swap, disk, battery health and cycle count.**
- **Per-core load**, split into performance and efficiency cores.
- **UDP ports** now appear. They were invisible before.
- **`--json`** for scripts and assistants that want the numbers rather than the screen.

Fixed:

- Process names were cut at the first space, so every Chrome process showed as "Google".
- Ports 5601, 5678 and similar were mislabeled as "rapportd".
- Bars showed two filled blocks at 0%, and columns drifted out of line after any colored value.
- The layout no longer runs off the edge on a narrow window.
- The kill hint now uses `-sTCP:LISTEN`. Without it the widely-copied one-liner can kill your browser
  instead of the server on that port.

Action required after updating: run `machine-monitor`, or
`python3 ~/.claude/skills/machine-monitor/scripts/main.py`. If you had a shell alias, a shortcut, or
a script pointing at `scripts/monitor.sh`, repoint it — that file no longer exists. Your settings
file is created for you on the first run.
