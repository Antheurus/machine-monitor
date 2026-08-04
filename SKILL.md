---
name: machine-monitor
description: >
  Live machine monitor — shows all running servers (listening ports), top CPU processes, top RAM processes, thermal load (pressure + power draw, no misleading °C), and an automatic WindowServer diagnosis when CPU is high, in an htop-style segmented terminal view with color-coded bars and kill instructions. Invoke this skill whenever the user asks to "see what's running", "check running servers", "monitor processes", "what's eating memory/CPU", "show running ports", "htop view", "kill a process", "check temperature", "how hot is my mac", "why is my mac hot", "why is WindowServer high", or wants a system overview of their machine. Also trigger proactively when the user asks to stop or restart a specific service and needs to identify its PID first.
---

## What this skill does

Runs `scripts/monitor.sh` — a self-contained bash script that renders a color-coded terminal dashboard:

1. **THERMAL LOAD** — Thermal pressure level, CPU/GPU power draw (mW). **No °C readings** — battery and CLI die temps mislead on Apple Silicon; use a thermal camera for chassis heat.
2. **SERVERS RUNNING** — every process listening on a port (PID, port, labeled service)
3. **TOP CPU USAGE** — top 15 processes by CPU%, color bars (red ≥15%, orange ≥5%, green <5%)
4. **WINDOWSERVER DIAGNOSIS** — **auto-appears when WS CPU > 15%**. Live validation of 5 factors (refresh rate, video decode, screen recording, transparency, displays) with verdicts — not guesses
5. **TOP RAM USAGE** — top 15 processes by %MEM, same color coding

Persistent mode (`-d`) auto-refreshes every 3 seconds. Press `r` to force refresh, `q` to quit.

## How to run

**Single snapshot:**
```bash
bash ~/.claude/skills/machine-monitor/scripts/monitor.sh
```

**Persistent live mode (auto-refresh every 3s):**
```bash
bash ~/.claude/skills/machine-monitor/scripts/monitor.sh -d
```

## Thermal load section — Apple Silicon reality

Apple Silicon Macs (M1/M2/M3+) **do not expose reliable CPU/chassis °C through any public macOS CLI**. Battery temp (~30°C) and `osx-cpu-temp` (often reads 0°C on M-series) **do not reflect how hot the laptop feels** — chassis can be 50–60°C while battery reads 31°C. This skill intentionally omits all °C readings.

| Metric | Source | Needs |
|--------|--------|-------|
| Thermal pressure (Nominal/Moderate/Heavy/Critical) | `powermetrics --samplers thermal` | passwordless sudo (see below) |
| CPU/GPU power draw (mW) | `powermetrics --samplers cpu_power,gpu_power` | passwordless sudo |

For actual surface heat, use a **thermal camera** — not CLI sensors.

### One-time setup for passwordless `powermetrics`

```bash
echo "$(whoami) ALL=(ALL) NOPASSWD: /usr/bin/powermetrics" | sudo tee /etc/sudoers.d/powermetrics
```

Script auto-detects this via `sudo -n -l /usr/bin/powermetrics` and uses `sudo powermetrics` transparently when set up.

**AI agents:** never quote battery °C or osx-cpu-temp as "how hot the Mac is". Report thermal pressure + power draw + top CPU processes instead.

## WindowServer diagnosis — automatic, real validation

When WindowServer CPU exceeds 15%, the script automatically runs **live checks** (not heuristics) for 5 factors:

| Factor | How it's checked | What it means |
|--------|------------------|---------------|
| Refresh rate | `system_profiler SPDisplaysDataType -json` parsed for `Hz` | If 120Hz → ProMotion compositing is the cause |
| Video decode | `pgrep VTDecoderXPCService` + summed CPU | If active → browser/app decoding video |
| Screen recording | known recorder apps in `ps aux` + `lsof` for `SCStream`/`screencaptured` | Confirmed if found |
| Transparency | `defaults read com.apple.universalaccess reduceTransparency` | OFF = blur/vibrancy compositing |
| Display count | `system_profiler` grep `Resolution:` | More displays = more pixels |

Output marks each as **← CAUSE** (definite), **← FACTOR** (contributing), or **✓** (not the cause). Don't speculate causes when this section ran — it has live data.

## Kill shortcuts (shown in the footer)

```bash
kill -9 <PID>                        # kill by PID (shown in yellow in the dashboard)
lsof -ti:<PORT> | xargs kill -9      # kill whatever is on a specific port
```

## When the user asks to kill a process

1. Run a snapshot (`monitor.sh`) to get current PIDs
2. Identify the PID from the relevant section
3. Confirm with the user which PID to kill before running `kill -9`

## Script location & dependencies

`scripts/monitor.sh` — bundled alongside this SKILL.md.

Required (built-in macOS): `ps`, `lsof`, `vm_stat`, `sysctl`, `system_profiler`, `defaults`, `awk`, `python3`.

Optional (richer data): `powermetrics` via passwordless sudo (thermal pressure + power draw).
