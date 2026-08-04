# machine-monitor

A [Claude Code](https://claude.com/claude-code) skill that renders a live, htop-style dashboard of a
macOS machine: listening ports, top CPU and RAM processes, real thermal load, and an automatic
WindowServer diagnosis when compositing goes hot.

Built for Apple Silicon, where the usual answers are wrong.

## Why it exists

Two things about macOS make "why is my Mac hot / slow" harder than it should be:

- **There is no reliable CPU or chassis temperature through any public macOS CLI on M-series.**
  Battery temp reads ~30 °C while the chassis is 50–60 °C, and `osx-cpu-temp` frequently reports
  0 °C. This skill therefore reports **no °C at all** — it shows thermal *pressure* and CPU/GPU
  power draw in mW, which are real. For surface heat, use a thermal camera.
- **`WindowServer` at high CPU gets guessed at.** ProMotion, video decode, screen recording,
  transparency, and display count are all plausible causes, and people pick one. This skill runs a
  live check for each of the five and labels them `← CAUSE`, `← FACTOR`, or `✓`.

## What it shows

| Section | Contents |
|---|---|
| **THERMAL LOAD** | Thermal pressure (Nominal/Moderate/Heavy/Critical) + CPU & GPU power draw in mW |
| **SERVERS RUNNING** | Every process listening on a port — PID, port, protocol, labeled service |
| **TOP CPU USAGE** | Top 15 by CPU%, color-coded bars (red ≥15%, orange ≥5%, green <5%) |
| **WINDOWSERVER DIAGNOSIS** | Auto-appears above 15% WS CPU — five live factor checks with verdicts |
| **TOP RAM USAGE** | Top 15 by %MEM, same color coding |

The header line carries device model, chip, core split (e.g. `10 (6P+4E)`), total RAM, load average,
and free memory.

## Install

As a Claude Code skill:

```bash
git clone https://github.com/Antheurus/machine-monitor.git ~/.claude/skills/machine-monitor
```

Claude then invokes it on prompts like "what's running", "why is my Mac hot", "show listening ports",
"what's eating memory", or "kill whatever is on port 3000".

## Run it directly

The script is self-contained — no Claude required.

```bash
# single snapshot
bash ~/.claude/skills/machine-monitor/scripts/monitor.sh

# live mode, refreshes every 3s (r = force refresh, q = quit)
bash ~/.claude/skills/machine-monitor/scripts/monitor.sh -d
```

## Thermal readings need one-time setup

`powermetrics` requires root. Without it the dashboard still works — the thermal section just goes
blank. To enable it:

```bash
echo "$(whoami) ALL=(ALL) NOPASSWD: /usr/bin/powermetrics" | sudo tee /etc/sudoers.d/powermetrics
```

The script probes for this with `sudo -n -l /usr/bin/powermetrics` and only uses `sudo` when the rule
exists — it never prompts for a password.

## Requirements

macOS. Everything else ships with the OS: `ps`, `lsof`, `vm_stat`, `sysctl`, `system_profiler`,
`defaults`, `awk`, `python3`. `powermetrics` is optional, as above.

## A note for AI agents reading this

Two failure modes worth knowing:

- Never quote battery °C or `osx-cpu-temp` as "how hot the Mac is". Report thermal pressure, power
  draw, and the top CPU processes instead.
- The `TOP CPU` column is `ps %CPU`, which on macOS is a **lifetime average since process start**,
  not instantaneous load. A long-running process can show a high number while currently idle. Confirm
  with `top -l 2` before blaming it.

## License

MIT — see [LICENSE](LICENSE).
