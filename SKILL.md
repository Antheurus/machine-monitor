---
name: machine-monitor
description: >
  This skill should be used when the user asks to "see what's running", "check running servers",
  "monitor processes", "what's eating memory/CPU", "show running ports", "htop view", "kill a
  process", "check temperature", "how hot is my mac", "why is my mac hot", "why is my mac slow",
  "why is WindowServer high", "what can I delete", "free up disk space", "find stale dev servers",
  or wants a system overview of their macOS machine. Also trigger proactively when the user asks to
  stop or restart a service and needs to identify its PID first. Reports real die temperature in °C,
  memory including compressed pages and swap, live per-process CPU, every listening port mapped to
  the project it was started from, Docker, disk, battery, reclaimable space, and orphaned automation
  browsers — and can terminate stale processes with confirmation.
---

## What this skill does

Runs `scripts/main.py` — a zero-dependency Python tool (system `python3`, nothing to install).

Dashboard sections, in render order: **VITALS** (CPU with per-core P/E split, memory, swap, disk,
network and disk throughput) · **THERMAL** (die temperatures, pressure, power draw) · **NEEDS
ATTENTION** (what to act on) · **SERVERS RUNNING** (every TCP and UDP listener with its project) ·
**DOCKER** · **TOP CPU** · **WINDOWSERVER DIAGNOSIS** (auto, above 15%) · **TOP RAM**.

## How to run

```bash
machine-monitor                      # single snapshot (alias to scripts/main.py)
machine-monitor -d                   # live: / filter, c·m·a sort, k kill, q quit
machine-monitor --json               # machine-readable, every number already parsed
```

**Prefer `--json` when answering a question rather than showing the user a screen.**

| Mode | Purpose |
|---|---|
| `--only <section>` | Render one section. Repeatable. Cheapest way to answer a narrow question. |
| `--space` | Reclaimable disk space: caches, `node_modules`, build output, Docker. Slow (~30s), walks project trees. |
| `--orphans` | Leftover automation browser sessions, grouped by profile. |
| `--history [hours]` | Recorded trends with sparklines. Every run records one sample. |
| `--save <name>` / `--diff <name>` / `--snapshots` | Compare the machine against a saved state. |
| `--reclaim` | Terminate dev servers idle past the stale threshold. Asks for typed confirmation. |
| `--kill-orphans` | Terminate leftover automation browsers. Asks for typed confirmation. |
| `--check` | Evaluate alert thresholds once and send a macOS notification. |
| `--watch-install [sec]` / `--watch-uninstall` / `--watch-status` | Background alert agent via launchd. |

Also: `-i/--interval`, `-n/--top`, `--no-color`, `--config`, `--dry-run`, `--yes`.

## Rules when acting on this tool's output

**Every number here has a specific meaning and several have a naive reading that is wrong.**
Before quoting a figure to the user or drawing a conclusion from it, consult
`references/metrics.md` — it covers what each metric actually measures, which ones supersede the
obvious source, and the readings that are structurally unavailable on Apple Silicon.

Three that matter most often:

- **Do not quote `RSS` as a process's memory.** The `MEM` column is physical footprint; a process
  that macOS has compressed shows a tiny RSS and a large footprint. Measured here: 3 MB RSS against
  2.2 GB footprint.
- **Do not read `CPU%` as a lifetime figure.** It is a live delta over the sample window.
- **Do not conclude "plenty of RAM free" from the memory row alone.** Read `SWAP` beside it.

## Killing things

Destructive paths live in `scripts/actions.py` and all obey the same contract: a target must come
from a candidate list, it is re-checked immediately before the signal, and nothing is signalled
without confirmation. SIGTERM first, SIGKILL only after a grace period.

When the user asks to kill something: run a snapshot, identify the PID, **confirm which PID with the
user**, then act. For a port, `lsof -nP -iTCP:<PORT> -sTCP:LISTEN -t` — `-sTCP:LISTEN` is not
optional, since a bare `lsof -ti:<PORT>` also matches client connections and can kill the user's
browser instead of the server.

## Configuration

Shipped defaults live in `scripts/config.ini`, in the repository. Personal overrides go in
`~/.config/machine-monitor/config.ini`, which is read second and wins key by key — anything omitted
there keeps the shipped value. Covers port labels and ranges, every colour threshold, refresh
interval, which sections render, project roots and hidden ports.

## Setup and degraded operation

Temperature needs no setup. Only thermal *pressure* and *power draw* require passwordless
`powermetrics`; without it those two fields are omitted and the dashboard prints the one-line command
to enable them. Missing Docker, sensors, or history each degrade to a hidden section rather than an
error — see `references/troubleshooting.md`.

## Verifying a change to this skill

`python3 tests/eval_scenarios.py` renders the dashboard against ten synthetic machines — hot, memory
full, swap thrashing, disk full, CPU pinned, no sensors, no memory source, empty, and absurd values —
and asserts on colours, alerts, empty states and line widths. Run it after any change to rendering,
thresholds, or collectors. `--show <scenario>` prints one for eyeballing.

## Additional resources

- **`references/metrics.md`** — what every metric actually measures, and the wrong readings to avoid.
- **`references/troubleshooting.md`** — degraded sources, permissions, and what each blank section means.
- **`tests/eval_scenarios.py`** — the failure-condition eval suite.
