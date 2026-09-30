---
name: machine-monitor
description: >
  This skill should be used when the user asks to "see what's running", "check running servers",
  "monitor processes", "what's eating memory/CPU", "show running ports", "htop view", "kill a
  process", "check temperature", "how hot is my mac", "why is my mac hot", "why is my mac slow",
  "why is WindowServer high", "what can I delete", "free up disk space", "find stale dev servers",
  "why is System Data so big", "kenapa System Data gede banget", "apaan 180GB System Data",
  "panas nih", "kok panas padahal nggak ngapa-ngapain", "what's grinding in the background",
  "chrome won't open", "chrome nggak bisa dibuka", "klik chrome nggak muncul apa-apa",
  or wants a system overview of their macOS machine. Also trigger proactively when the user asks to
  stop or restart a service and needs to identify its PID first. Reports real die temperature in °C,
  memory including compressed pages and swap, live per-process CPU, every listening port mapped to
  the project it was started from, Docker, disk, battery, reclaimable space, and orphaned automation
  browsers. Catches stuck background work — a Settings pane, Spotlight, Photos or an iCloud sync
  grinding for hours with no window to show it — which is the usual answer when the machine feels
  warm but the temperatures read normal. Also groups any process family into sessions ("kill the
  playwright chromes", "kill the test browsers", "what automation is still running", "list MCP
  servers") and terminates them one session at a time with confirmation, leaving the user's own apps
  untouched.
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
| `--clones` | Leftover code-sign clone litter — the usual cause of an absurd "System Data" figure. |
| `--clean-clones` | Remove the clones no process holds open, and report the *measured* reclaim. |
| `--orphans` | Leftover automation browser sessions, grouped by profile. |
| `--sessions [family]` | Every process-family session: root pid, process count, footprint. Optionally one family. |
| `--kill-session <root-pid>` | Terminate one session by its root pid. Repeatable. Asks for typed confirmation. |
| `--history [hours]` | Recorded trends with sparklines. Every run records one sample. |
| `--save <name>` / `--diff <name>` / `--snapshots` | Compare the machine against a saved state. |
| `--reclaim` | Terminate dev servers idle past the stale threshold, plus detached dev stacks that hold no port. Asks for typed confirmation. |
| `--kill-orphans` | Terminate leftover automation browsers. Asks for typed confirmation. |
| `--check` | Evaluate alert thresholds once and send a macOS notification. |
| `--watch-install [sec]` / `--watch-uninstall` / `--watch-status` | Background alert agent via launchd. |

Also: `-i/--interval`, `-n/--top`, `--no-color`, `--config`, `--dry-run`, `--yes`.

## Rules when acting on this tool's output

**Every number here has a specific meaning and several have a naive reading that is wrong.**
Before quoting a figure to the user or drawing a conclusion from it, consult
`references/metrics.md` — it covers what each metric actually measures, which ones supersede the
obvious source, and the readings that are structurally unavailable on Apple Silicon.

The ones that matter most often:

- **Do not quote `RSS` as a process's memory.** The `MEM` column is physical footprint; a process
  that macOS has compressed shows a tiny RSS and a large footprint. Measured here: 3 MB RSS against
  2.2 GB footprint.
- **Do not read `CPU%` as a lifetime figure.** It is a live delta over the sample window.
- **Do not conclude "plenty of RAM free" from the memory row alone.** Read `SWAP` beside it.
- **Do not quote a `du`-derived size as reclaimable disk.** `du`, `--space` and macOS's own Storage
  pane all bill an APFS clone at full size. See below.
- **Do not report swap percentage as a before/after figure.** macOS shrinks the swap *file* as
  pressure falls, so both halves of the ratio move together and a real improvement reads as none.
  Measured here: `7.95G / 9.2G` → `4.32G / 5.0G` is 86% → 84%, and 3.6 GB of paging going away.
  Quote the absolute used figure; treat the file shrinking as itself the evidence.

## A warm machine with ordinary temperatures

Answer "why is it hot" from **NEEDS ATTENTION**, not from the °C in THERMAL. A die at 50 °C with
`Nominal` pressure rules out a thermal fault; it does not rule out sustained work, and sustained work
is what actually warms the chassis.

The section flags **stuck background work** — a process that is busy in this sample *and* has been
busy across its entire life. Both conditions are required, which is what separates a daemon looping
forever from a compile that is briefly busy. The usual culprits have no window, no port and little
memory, so every other section renders them as unremarkable: a Settings pane left open, Spotlight
indexing, Photos analysing, an iCloud sync. Known daemons are named with a cause and a remedy;
anything else is reported with its pid.

Corroborate with `sys` CPU time and `kernel_task` rather than temperature — a stuck daemon shows up
there long before it shows up in degrees.

**Quote power draw, not °C, as the before/after of a heat fix.** The die can sit at 46 °C and
`Nominal` the whole time while the chassis is warm; what the user feels is watts. Measured
2026-08-26: `thermal.cpu_mw` 16,162 → 1,094 mW after a cleanup, with the temperature reading
unchanged — quoting °C would have made a 15x fix look like nothing. Load average is the other honest
figure (20.66 → 2.18 the same day). Free RAM is not: macOS keeps it near zero before and after.

**A graceful quit takes minutes on a thrashing machine, and that is not a hang.** Every process has
to be paged back in from SSD just to be told to exit — Chrome went 167 → 161 processes in a full
minute at 94% swap. Wait; do not escalate to SIGKILL on the assumption it is stuck.

## Apparent size is not disk space

A Mac reporting **System Data 180.52 GB** with 79 GB free is almost always carrying code-sign clone
litter, and every cheap way to measure it overstates the win by two orders of magnitude.

macOS clones an app bundle to verify its signature, files it under
`/private/var/folders/<x>/<y>/X/<bundle-id>.code_sign_clone/`, and often fails to reap it. Measured
here 2026-08-10: **258 copies of `Google Chrome.app`, 652 GB apparent, accruing ~32/day** — on a
volume with 375 GB used, which is the tell. `du -sh -x /private/var/folders` answered **609 G**.

**Deleting all 258 freed 2.4 GB.** The blocks were shared with the original the whole time.

So when answering a storage question:

- **Quote `df`/`statvfs`, never `du`.** Treat any `du` total — including the one `--space` prints —
  as an upper bound. A directory reporting more bytes than the volume physically holds is the
  diagnosis, not a glitch to route around.
- **Take a free-space reading before *and* after any cleanup.** `--clean-clones` does this and prints
  both figures side by side. Never report the size of what was deleted as what the user got back.
- **Say plainly that the Storage pane's number will drop while free space barely moves.** That is the
  honest outcome and it is still worth doing — the panel stops lying.

`--clean-clones` skips any clone a process currently holds open (`lsof +D`), so it runs safely
without quitting the app. When `lsof` cannot answer, the bucket is refused rather than guessed at —
`IN USE ?` in the report means exactly that. Sparse files invert the same trap: `Docker.raw` measured
494 GB apparent against 13.4 GB of real blocks.

## Process families and sessions

A **session** is one root process plus every descendant it spawned — a Playwright daemon with its
browser and that browser's helpers is one row, not thirteen. Membership comes from **ancestry**:
only the root is matched on argv, and helpers are claimed because of who their parent is. Matching
each process on its own argv is the obvious approach and it is wrong — helpers do not repeat
`--user-data-dir`, so 8 of 40 automation processes classified as the human's own Chrome.

Families are data, in `[process_families]` in `config.ini`:

```ini
label = marker, marker, ...
```

A marker prefixed `=` must equal a whole argv token; anything else is a plain substring. Both forms
are load-bearing — a profile path is a fragment, while the bare word `mcp` as a substring also
matched `Cursor Helper: mcp-process`. Adding a family is one line; setting an existing label
replaces its shipped markers. Ships with `automation-browser` and `mcp-server`.

Use `--sessions` to audit and `--kill-session <root-pid>` to remove one. Prefer this over
`--kill-orphans` whenever some sessions must survive: `--kill-orphans` is all-or-nothing.

## Where the memory actually is

On this class of machine (16 GB, several agent sessions a day) exhaustion is almost never the user's
browser. It is leftovers from sessions that have already ended, and the categories rank in the
opposite order to how loudly they alert. Measured 2026-08-26:

| Source | Found by | Measured |
|---|---|---|
| Orphaned automation browsers | `--orphans` / `--sessions` | 16 sessions, 5.8 GB |
| Detached dev stacks, no port | NEEDS ATTENTION, `--reclaim` | 6 stacks, 4.5 GB (2026-09-11) |
| Open agent sessions + MCP servers | NEEDS ATTENTION | 12 sessions, 5.28 GB (2026-08-16) |
| Stale listening dev servers | `--reclaim` | 5 servers, 81 MB |

**A leftover automation browser also breaks the user's own Chrome.** macOS sees "Google Chrome"
already running, so with the user's Chrome closed the Dock icon activates a windowless automation
instance and nothing opens — reported as "Chrome won't open", with three 2.5-3.5-day-old
playwright-cli daemons as the whole cause (2026-09-30). NEEDS ATTENTION names this once an
automation session is adopted by launchd and past `stale_server_hours`.

**Run `--orphans` before `--reclaim`.** The stale-server alert is the loudest and recovered ~1.4% of
what the orphan sweep did.

**A dev stack that lost the port race is invisible to every listener-based view.** A second
`just dev` keeps its whole `pnpm → nest watch → node` tree alive without ever binding, so SERVERS
RUNNING and the stale-server alert cannot see it. `detached_stacks` finds trees whose root is a dev
runner (`[sessions] dev_runners`), was adopted by launchd, sits under a project root, is past
`stale_server_hours`, and holds no listener anywhere in the tree. `--reclaim` signals the whole
tree, supervisor included — killing only a watcher's child gets it respawned — and re-checks each
pid's `lstart` before the signal.

**An agent session is not an orphan.** `--sessions` shows `gitnexus mcp` rows days old; all 13 seen
on 2026-08-16 were children of live `claude` processes. The agent-session alert reports the count
and combined memory (outermost session only, helpers included) once it reaches
`agent_sessions_warn`, because closing idle tabs is then the biggest single lever — and it is the
user's call, so the tool reports it and never kills it.

## Killing things

Destructive paths live in `scripts/actions.py` and all obey the same contract: a target must come
from a candidate list, it is re-checked immediately before the signal, and nothing is signalled
without confirmation. SIGTERM first, SIGKILL only after a grace period.

**The re-check needs an argv mark, not just a name, for anything multi-process.** `refuse_reason`
compares `ps comm=` against the caller's copy to catch a recycled pid, and that is useless for an
app where every process shares one executable: the user's Chrome and a Playwright Chrome both report
`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`. Measured against the live machine,
the name-only guard **permitted** signalling the human's browser; passing `expected_argv_mark` (a
throwaway `--user-data-dir`, a daemon's session name) refused it while still allowing the genuine
target. Any new caller of `terminate`/`terminate_all` passes a mark when it has one — targets may be
`(pid, name)` or `(pid, name, mark)`.

**A listening socket in a project directory is not proof of a dev server.** An agent CLI started
from a repo leaves helpers that listen on a port, sit in that repo's cwd, and outlive any sane stale
threshold — so every test `--reclaim` applies says yes, and killing one breaks the tools of a session
that is still running. `mark_session_owned` resolves each listener's ancestry and spares anything
descended from a live `claude`/`codex`/`cursor`/`windsurf` (the list is `[sessions] owners` in
config). Ancestry, never the process's own argv: a helper does not repeat its parent's identity.
Spared targets are **printed with the reason**, never filtered out silently. A leftover whose session
has exited reparents to launchd, so the guard costs a genuine stale server nothing.

**Never build a kill list by sorting listeners on age.** Doing that by hand here put the user's own
terminal, their cloud-sync client and two live-session helpers in the candidate set — the terminal
being the one that would have taken every backgrounded process with it. Use `--reclaim`, or match on
what a process *is* (its full argv path and its ancestry).

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

`python3 tests/test_clones.py` covers clone detection and removal against a synthetic tree, proving
each guard in **both** directions — a check that only ever refuses is indistinguishable from a broken
one. Run it after any change to `clones.py` or to the directory half of `actions.py`.

`python3 tests/test_grind_and_sessions.py` covers stuck-daemon detection and the live-session guard,
each paired with the lookalike it must let through — a fresh spike, a process that idled most of its
life, a leftover reparented to launchd, a binary whose name merely contains `claude`. Run it after
any change to `grinding`, `mark_session_owned`, or the `--reclaim` candidate set.

`python3 tests/test_detached_stacks.py` covers the portless-stack finder, the agent-session total,
and `cwd_for_pids` surviving a root-owned pid in its batch (lsof exits 1 on any unreadable pid, and
the old path dropped every answer with it). `python3 tests/test_orphan_naming.py` proves an orphan
whose executable path contains spaces is still killable and a recycled pid is still refused.

## Additional resources

- **`references/metrics.md`** — what every metric actually measures, and the wrong readings to avoid.
- **`references/troubleshooting.md`** — degraded sources, permissions, and what each blank section means.
- **`tests/eval_scenarios.py`** — the failure-condition eval suite.
- **`tests/test_clones.py`** — clone detection, removal guards, and the apparent-vs-real measurement.
- **`tests/test_grind_and_sessions.py`** — stuck-daemon detection and the live-session kill guard.
- **`tests/test_detached_stacks.py`** — portless leftovers, agent-session memory, foreign-pid cwd lookup.
