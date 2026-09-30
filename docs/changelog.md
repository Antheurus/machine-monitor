# Changelog

## v2.5.1 — `--space` no longer hides a cache it cannot fully read

- **Large caches stop disappearing from `--space`.** If a single file inside a folder couldn't be
  read, the whole folder was left off the report — a 27 GB Go build cache went missing that way, and
  it turned out to be the biggest thing on the disk. It is listed now.

## v2.5.0 — Finds the leftovers that were hiding from it

- **Dev stacks that never got a port now show up.** When you start a dev server twice, the second
  copy often loses the port but keeps running — sometimes for a week, holding gigabytes. It used to
  be invisible everywhere in the dashboard. It now appears under NEEDS ATTENTION, and
  `--reclaim` offers to stop it (the whole process tree, not just one piece that would restart).
- **It tells you when open agent sessions are the memory problem.** Past six open Claude/Codex/
  Cursor sessions it reports how many and how much memory they hold together with their helpers.
  It never closes them for you — that is your call.
- **Small memory figures read correctly.** An alert that said "holding 0.0G" for a 47 MB server now
  says "28M" or whatever the real number is.
- **The kill hints in the footer no longer suggest `kill -9`.** Plain `kill` lets a server shut
  down cleanly; force-killing first can leave its port stuck.
- **Killing a leftover automation browser whose app path has spaces in it now works** — it used to
  be refused by the tool's own safety check.
- New setting: `agent_sessions_warn` under `[thresholds]`, and `dev_runners` under `[sessions]` if
  your stacks start from something other than just/pnpm/npm/bun/node and friends.

## v2.4.0 — The README now shows you what the tool looks like

- **There is a demo GIF at the top of the README.** Previously you had to install it to find out
  what it rendered.
- **The machine in that GIF is made up, on purpose.** A screen recording of the real dashboard would
  publish whatever you happen to be running — your project folder names, your Docker container
  names, your automation sessions. The demo invents a plausible machine instead, so nothing of
  yours ends up in the picture.
- **You can regenerate it yourself** with `python3 tests/ansi_to_gif.py`. No screen recorder and no
  extra software to install; it draws the frames directly. `python3 tests/demo_render.py --loop 60`
  gives you the animated version in your own terminal if you would rather record it.

No action required, and nothing about how the tool reads your machine has changed.

## v2.3.0 — Find out why the Mac is warm when the temperatures look fine

- **New: it now spots background work that is stuck.** Some things macOS runs have no window, so
  there is nothing to close and nothing to notice — they just quietly use a chunk of your CPU for
  hours. On this Mac, leaving the **System Settings > Storage** panel open had three of them running
  for **7 hours 48 minutes**. The machine felt warm, and every temperature reading looked completely
  normal, so nothing pointed at the cause.
- **It tells you what started it and how to stop it.** For the ones it recognises — the Storage
  panel, Spotlight indexing, Photos analysing your library, iCloud syncing, Time Machine — you get a
  plain sentence naming the cause and the fix. Anything else is reported with its process ID.
- **It will not flag your actual work.** A build or a video export is busy too. Something is only
  reported when it has been busy *the whole time it has existed*, which a normal task never is.
- **Fixed: cleaning up old dev servers could kill a running Claude/Codex session's helpers.** If you
  start an agent CLI inside a project folder, it leaves helpers that look exactly like a forgotten
  dev server — same folder, listening on a port, running for days. `--reclaim` would have terminated
  one. It now checks what started each process and skips anything belonging to a session that is
  still open, listing what it skipped and why rather than quietly leaving it out.
- **Note on the swap figure after a cleanup:** the percentage can barely move even when things
  genuinely improved, because macOS shrinks the swap file at the same time. The tool now says so, and
  quotes the actual gigabytes instead.

## v2.2.0 — Find out why "System Data" says 180 GB, and get an honest answer

- **New `--clones`: the usual reason your Mac claims a huge "System Data".** Every time an app's
  signature is checked, macOS copies the whole app into a temp folder and is supposed to throw the
  copy away. Often it doesn't. On this Mac there were **258 copies of Google Chrome** stacked up over
  eight days, and Settings was counting all of them.
- **`--clean-clones` clears them, and tells you the truth about what you got back.** Those copies
  share storage with the real app, so the disk only returns a small fraction of the headline number —
  here 652 GB of copies gave back 2.4 GB. The tool measures free space before and after and reports
  the real figure, instead of adding up what it deleted and calling that a win. Expect the Storage
  pane to drop a lot while free space barely moves. That is the correct outcome: the panel stops
  lying to you.
- **You don't have to quit the app first.** Any copy currently in use is left alone. If that can't be
  determined for certain, the tool refuses that app rather than guessing.
- **`--space` no longer overpromises.** It used to state the exact disk percentage you'd reach after
  clearing everything it listed. It now says "no better than", because the same shared-storage effect
  applies there too.

No action required after updating.

## v2.1.0 — Clean up leftover browsers and servers one session at a time

- **Kill exactly the sessions you mean to.** `--sessions` lists every group of related processes —
  a Playwright browser and all its helpers count as one row, not thirteen — and
  `--kill-session <root pid>` removes one of them. Previously the only option was `--kill-orphans`,
  which took all of them or none, so a leftover session could not be cleared while a browser you
  were still using stayed open.
- **Your own apps are no longer mistaken for automation.** Grouping now follows which process
  started which, instead of guessing from each process's own command line. That guess was putting 8
  of 40 automation processes in the same bucket as your real Chrome.
- **A safety hole in the kill path is closed.** The check that stops the tool signalling the wrong
  process compared program names, and every window of one app shares a name — so your own Chrome and
  a test Chrome looked identical to it. It now also checks the process's start time, which is unique
  to each one.
- **Memory figures in the one-shot views were far too low.** `--orphans` and the other single-run
  modes were reporting a smaller number than the real one — 1.2 GB for what was actually 2.70 GB of
  leftover browsers. Fixed.
- **Add your own process groups** in `config.ini` under `[process_families]`, one line each.
- No action required after updating.

## v2.0.1 — The mystery 4 GB process now tells you which app it belongs to

- **The biggest memory hog is no longer anonymous.** A process called
  `com.apple.Virtualization.VirtualMachine` regularly sits at the top of TOP RAM using several
  gigabytes. That name comes from a macOS framework, not from any app you installed — Docker Desktop,
  UTM, Podman and others all show up under it, so there was no way to tell which one to quit. It now
  reads `Docker · com.apple.Virtualization.VirtualMachine`, naming the real owner.
- **`--json` was ranking TOP RAM the wrong way.** It sorted by RSS while the on-screen table sorted by
  actual memory footprint, so the two could disagree about which process was worst — and RSS is the
  measurement that hides compressed processes. Both now use footprint, and each row includes a
  `memory` field so you can see the number it was ranked on.
- No action required after updating.

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
