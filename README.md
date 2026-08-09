# machine-monitor

A [Claude Code](https://claude.com/claude-code) skill — and a standalone CLI — that renders a live,
htop-style dashboard of a macOS machine: real die temperature, honest memory accounting, live
per-process CPU, every listening port mapped to the project it came from, Docker, disk, battery, and
an automatic WindowServer diagnosis when compositing goes hot.

Built for Apple Silicon, where the usual answers are wrong. Zero dependencies — system `python3`.

## Why it exists

Three things about macOS make "why is my Mac hot / slow" harder than it should be, and the obvious
answer is wrong in all three.

- **`ps aux`'s `%CPU` is a lifetime average since the process started**, not current load. A dev
  server that has been up for four days shows a low, flat number while pinning a core right now.
  This tool differences each process's CPU time over a real sample window, which is what `top` does
  internally.
- **"Memory used" is usually computed as active + wired, which ignores compressed memory** — the
  single largest category on a machine under pressure. Measured on the development machine: active +
  wired read 5.7 GB while the true figure including the compressor was 12.3 GB of 16 GB, with swap
  88% full. The naive number said there was plenty of headroom while the machine was thrashing.
- **`WindowServer` at high CPU gets guessed at.** ProMotion, video decode, screen recording,
  transparency and display count are all plausible, and people pick one. This runs a live check for
  each and labels them `← CAUSE`, `← FACTOR`, or `✓`.

### On temperature

Earlier versions of this tool stated that Apple Silicon exposes no reliable die temperature and
deliberately printed none. **That was wrong**, and it is worth being precise about why: it is true
that no *public* API exposes it — `powermetrics` reports thermal pressure only, and its `smc` sampler
does not exist on arm64. But the private `IOHIDEventSystemClient` sensor services do expose it, need
no root and no install, and are what Stats.app, btop and macmon all read. This tool now reads them
directly through `ctypes` and prints real °C.

One caveat carried over from that work: `PMU tcal` looks like a sensor and is a calibration constant.
It never moves, and averaging it into the die temperature drags the number several degrees off.

## What it shows

| Section | Contents |
|---|---|
| **VITALS** | CPU% with a per-core sparkline split P-core / E-core, memory (wired · active · compressed · inactive), swap, disk |
| **THERMAL** | CPU die, GPU, battery and SSD °C, thermal pressure, CPU/GPU power draw |
| **NEEDS ATTENTION** | Swap thrashing, disk filling, two processes serving one project, servers up for days |
| **SERVERS RUNNING** | Every listening TCP **and UDP** socket — port, PID, process, age, memory, and the project directory it was started from |
| **DOCKER CONTAINERS** | Name, live CPU and memory, status, image |
| **TOP CPU** | Live CPU%, sampled over a real interval |
| **WINDOWSERVER DIAGNOSIS** | Appears automatically above 15% WS CPU — five live factor checks with verdicts |
| **TOP RAM** | Resident size per process |

The project column is what tells two identical `next-server` processes apart — which worktree each
one was launched from.

## Install

As a Claude Code skill:

```bash
git clone https://github.com/Antheurus/machine-monitor.git ~/.claude/skills/machine-monitor
```

Claude then invokes it on prompts like "what's running", "why is my Mac hot", "show listening ports",
"what's eating memory", or "kill whatever is on port 3000".

## Run it directly

```bash
# single snapshot
python3 ~/.claude/skills/machine-monitor/scripts/main.py

# live mode, r refreshes now, q quits
python3 ~/.claude/skills/machine-monitor/scripts/main.py -d

# JSON, for scripts and agents
python3 ~/.claude/skills/machine-monitor/scripts/main.py --json
```

`-i/--interval` sets the refresh, `-n/--top` the rows per table, `--no-color` disables ANSI,
`--config` prints the config path.

## Configuration

`~/.config/machine-monitor/config.json`, written on first run. Port labels, port ranges, every color
threshold, refresh interval, which sections render, project roots, hidden ports. Unknown or missing
keys fall back to the defaults, so the file never has to be complete or current.

## Thermal pressure and power draw need one-time setup

Temperature works with no setup. Only thermal *pressure* and *power draw* come from `powermetrics`,
which needs root:

```bash
echo "$(whoami) ALL=(ALL) NOPASSWD: /usr/bin/powermetrics" | sudo tee /etc/sudoers.d/powermetrics
```

The tool probes with `sudo -n` and never prompts for a password. Without the rule those two fields
are omitted and the dashboard prints this command instead.

## Killing things safely

```bash
kill -9 <PID>
lsof -nP -iTCP:<PORT> -sTCP:LISTEN -t | xargs kill -9
```

`-sTCP:LISTEN` is load-bearing. The widely-copied `lsof -ti:<PORT> | xargs kill -9` also matches
*client* connections to that port — pointed at a port your browser is talking to, it kills the
browser and leaves the server up, so the "restart" appears to do nothing and destroys something else.

## Requirements

macOS, Python 3.9+ from the system. No third-party packages. Uses `ps`, `lsof`, `vm_stat`, `sysctl`,
`pmset`, `ioreg`, `system_profiler`, `defaults`, and `docker` when it is installed. `powermetrics`
optional, as above.

## License

MIT — see [LICENSE](LICENSE).
