# machine-monitor Progress

## Session — 2026-08-10 — v2.1.0 (process-family sessions, and a kill guard that actually guards)

Started as a request to kill leftover test/Playwright Chromes without touching the user's own
browser. A throwaway script did that job — 7 sessions, 40 processes, 2.70 G, `default` deliberately
spared — and the user then asked why the capability was not general. It is now part of the tool that
already owned this job rather than a second skill beside it, because `machine-monitor` had
`--orphans`, `--kill-orphans` and the kill contract in `actions.py`, and one of them was unsafe.

**The guard did not guard.** `actions.refuse_reason` catches pid reuse by comparing `ps comm=`
against the caller's copy. Every process of a multi-process app shares one executable, so the user's
Chrome and a Playwright Chrome both report
`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` — byte-identical. Driven against the
live machine with pid 462 (the human's 7-day-old browser) and the name a caller would have captured
from an automation Chrome, the old guard returned `''`: it **permitted** signalling the user's
browser. `refuse_reason` now also takes `expected_start` (the process's `lstart`, which identifies
an instance and so settles reuse outright, and works for a helper whose argv is indistinguishable)
and `expected_argv_mark` (a literal only the intended target carries, which catches a caller
confusing two live processes). Same probe, new guard: refused, while still allowing the genuine
automation pid — a check that only ever refuses proves nothing.

**Grouping moved from argv to ancestry.** `orphan_automation` matched each process on its own argv,
and a browser's helpers do not repeat `--user-data-dir`, so 8 of 40 automation processes classified
as the human's own Chrome. `collect.sessions()` matches only a *root* on argv and claims every
descendant by parentage, absorbing a root that sits under another root — which folds a Playwright
daemon and the browser it launched into one session instead of two. Families are data
(`[process_families]` in `config.ini`), shipping `automation-browser` and `mcp-server`, both verified
live rather than imagined. A marker prefixed `=` must equal a whole argv token: the bare substring
`mcp` also matched `Cursor Helper: mcp-process`, i.e. the user's editor. New `--sessions [family]`
audits and `--kill-session <root-pid>` removes one, which `--kill-orphans` could not do — it is
all-or-nothing, and the whole point here was sparing one live session.

**Three bugs found while verifying, each of which looked like success.** A blanket string replace
routing call sites through a new `sample_with_footprints` helper rewrote the helper's own body into
a self-call; caught by an AST check for recursion rather than by reading. Then the dry run reported
`1 process(es) would be terminated` for a 7-process session — twice, for two different reasons. The
session's root binary was passed as `expected_name` for every pid, so each descendant refused with
`pid now belongs to Google Chrome, not node`; and after fixing that, a name derived by splitting
argv on the first space turned `Google Chrome Helper (GPU)` into `Google` and every descendant
refused again. That second one is the trap `ProcessSampler`'s own docstring already warns about,
reintroduced. Both reported the root as terminated, which reads as a cleaned-up session and is not.
Names and start times now come from one `ps -eo pid=,lstart=,comm=` read, basenamed as a whole.

**A separate defect surfaced in the same area**: every one-shot mode sampled processes without
merging footprints, so `Process.memory` silently fell back to RSS. The orphan view reported a 2.70 G
pile of leftover browsers as 1.2 G, and a compressed 2.2 G MCP server as 3.8 M — 580× low. All four
one-shot call sites now go through `sample_with_footprints`.

Verified: `tests/eval_scenarios.py` 11/11 plus a new unit-check phase covering ancestry grouping,
the user's own Chrome staying out of every session, the `Cursor Helper: mcp-process` false positive,
the argv guard in both directions against a real spawned process, whole-session targeting on a live
parent+child, and `_identity` parsing a spaced executable name. Each new check was proved against a
reintroduced bug in a throwaway copy: root-name gives `dry run planned 1 of 2 pids`, space-split
gives `executable name parsed as 'Google'`. A spaced-name process cannot be spawned to test the
latter — macOS SIGKILLs a copied system binary on signature check and it is `<defunct>` before `ps`
sees it — so that one asserts on crafted `ps` output instead. Live: `--sessions` shows 17 sessions
/ 3.9 G with the Playwright session correctly folded to 7 processes; `--kill-session 79985
--dry-run` plans all 7. Follow-up: an interactive session picker in live mode (`-d`) was scoped out.

---

## Session — 2026-08-09 (cont) — v2.0.1 (attribute shared framework helpers to their owning app)

The user asked for a temperature check, got a clean thermal report (CPU die 50 °C, GPU 53 °C,
pressure Nominal) alongside a swap emergency (95% of 14 G), and then caught a wrong attribution in
the answer: the 4.0 G row at the top of TOP RAM was reported as UTM, and UTM had been uninstalled.
Root cause is that `com.apple.Virtualization.VirtualMachine` is Apple's shared Virtualization.framework
XPC service — the executable name identifies the *framework*, and launchd reparents the service so
its `ppid` is 1, killing both of the usual attribution routes. Docker Desktop, UTM's Apple-VZ mode,
Podman and VirtualBuddy all present as that same anonymous multi-gigabyte process, so the row that
matters most on a memory-pressured machine is the one row nobody can name. Here it was Docker
Desktop, proved from the fd table: `/Applications/Docker.app/Contents/Resources/linuxkit/kernel` and
`~/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw`, both held open by pid 37870,
with no `UTM.app` present anywhere and no `qemu`/`utmd` running (only an empty 32 KB
`com.utmapp.QEMUHelper` container left behind by the uninstall).

The fix is a `GENERIC_HELPERS` registry in `collect.py` plus `owner_app(pid)`, which reads
`lsof -p <pid> -Fn` and takes the **most frequently referenced** `*.app` bundle outside `/System` —
most-frequent rather than first-seen because a helper incidentally opens a font or resource from an
unrelated bundle. Results are cached on `(pid, name)`, and the cost is 25 ms for the one process on
this machine that matches. Two traps closed while building it. `lsof` exits non-zero whenever any
single fd is unreadable, and the module's `run()` helper returns `''` on a non-zero exit, so the
resolver calls `subprocess.run` directly and reads stdout regardless of return code. More
importantly, the owner is a **new field** rather than a rewrite of `Process.name`: `actions.refuse_reason`
re-reads `ps comm=` and compares it against the caller's copy to catch pid reuse, so decorating the
name would have made every kill of a resolved helper refuse itself with `pid now belongs to
com.apple.Virtualization.VirtualMachine, not Docker · com.apple.…` — a fabricated pid-reuse warning
on the kill path. That was verified by calling `refuse_reason` both ways: raw name returns `''`,
decorated name returns the bogus refusal. Rendering composes `display_name` as `owner · name`, owner
first so the attribution survives the PROCESS column truncating the tail at 45 chars.

A second, independent defect surfaced in the same area: `to_json` ranked `top_ram` by `p.rss` while
the rendered table ranks by `p.memory` (footprint), so the JSON output — the mode the skill's own
SKILL.md recommends for answering questions — used exactly the ranking the docs call structurally
unable to find the largest consumer. It now sorts by footprint and emits a `memory` field, since
`dataclasses.asdict` skips the property the sort is based on.

Verified: `tests/eval_scenarios.py` 11/11 (a new `shared-helper` scenario asserts the owner renders,
that `Process.name` is *not* rewritten, and that a 4 G footprint ranks first in TOP RAM), and the new
scenario was run against `HEAD`'s scripts in `/tmp/mm_pre` — it fails there with "a shared helper
rendered without its owning app", so the check can distinguish. Live: TOP RAM now shows
`Docker · com.apple.Virtualization.VirtualMach…` at 4.0 G, and `--json` reports
`owner='Docker', memory=4105M, rss=543M`. Files: `scripts/collect.py`, `scripts/main.py`,
`tests/eval_scenarios.py`, `references/metrics.md`. Follow-up: `GENERIC_HELPERS` holds one entry;
`com.apple.WebKit.WebContent` is the obvious second, but no instance was running to verify against,
and this corpus does not document unverified behaviour.

---

## Session — 2026-08-09 (cont) — v2.0.0 (live-mode latency: ~3s → 3ms)

The user reported that live mode waits three seconds after every keypress and called it a design
flaw. It was, and it was three separate flaws that happened to compound.

**Input was queued behind collection.** The live loop called `snapshot()` itself, so a keypress was
answered only after the next full gather. Collection now runs on a background thread (`SnapshotFeed`)
that publishes the newest snapshot; the render loop polls input every 50 ms and re-renders from
whatever snapshot is already in hand. Sorting and filtering need no new data at all, so they are
answered immediately — the two latencies were never related and are no longer related in the code.

**Every collector ran at the fastest one's cadence.** Measured per collector: `docker` 1.79 s,
`powermetrics` 1.45 s, `top` 1.10 s, and the remaining eight together 0.79 s. But the Docker
container list changes on the scale of minutes, and battery, disk and listeners likewise. Each
collector now carries a TTL (`Monitor.TTL`) and is reused inside it. A warm frame went from 2.50 s to
0.15 s.

**And the render function was collecting.** Profiling `build()` at 288 ms showed `select.poll` at
2.65 s of cumulative time and 40 `fork_exec` calls *inside a draw function*: the WindowServer
diagnosis was shelling out to `system_profiler`, `ps` and `defaults` while drawing. That is a
collection concern that had been written into the render layer, and it only surfaced because it is
gated on WindowServer CPU being high, which it happened to be on this machine. Moved into
`snapshot()` behind a 15 s TTL. `build()` is now 1.7 ms, helped further by memoising `_char_width`,
which a single frame was calling ~14,000 times over an alphabet of a few dozen characters.

Measured through a pty, keypress to visible change: `m` 3 ms, `c` 3 ms, `a` 2 ms, `/` 1 ms — against
roughly 3,000 ms before. A one-shot run still costs ~2.8 s because it has no warm cache to reuse,
which is correct: there is nothing to amortise against. `History` gained
`check_same_thread=False` plus a lock, since the collector thread now writes it. Re-verified: all
modules compile, 10/10 eval scenarios, every mode returns its correct exit code, and the WindowServer
section still renders from the relocated source.

---

## Session — 2026-08-09 (cont) — v2.0.0 (twelve features, config.ini, eval suite)

Second half of the same session. The user reviewed a questionnaire of candidate improvements and
selected every option, so this entry covers twelve features plus two mid-flight requests.

**Configuration moved out of Python and into `scripts/config.ini`**, parsed by `configparser` and
overlaid with `~/.config/machine-monitor/config.ini`. The motivation was that the shipped defaults
were leaking one machine's specifics into a public repository — the port ranges 3400/5400/6400/8400
encode the author's personal Docker port convention and were removed; `project_roots` was
generalised. The user file is created as a commented template holding **zero active lines**, which is
the point: an override file that is a full copy of the defaults masks every future update to the
repo. Verified three ways — an override wins, an unmentioned key keeps its shipped value, and a
malformed user file degrades to shipped defaults with the reason reported in the footer rather than
being silently ignored. A `machine-monitor` zsh alias was added, and `monitor.py` was renamed to
`main.py` at the user's request; the rename immediately surfaced a name collision in the eval suite,
where a local `def main()` shadowed the imported module, fixed with `import main as app`.

**Memory ranking switched from RSS to physical footprint**, which is the single largest correctness
win of the session. RSS cannot find the biggest consumers on a machine under pressure: once macOS
compresses a process's pages they stop being resident. Measured simultaneously — `node` 3 MB RSS
against 2269 MB footprint (681×), `next-server` 9 MB against 2076 MB, `Maccy` 30 MB against 1982 MB.
The old table was ranking on a number that structurally hides exactly what it exists to find.
`proc_pid_rusage` via ctypes was tried first and returns EPERM on 59 of 60 sampled pids (it needs a
private codesign entitlement), so the source is `top -l 1 -stats pid,mem`; cost is ~0.9 s regardless
of row count, absorbed by the existing thread pool. RSS is retained as a dimmed column that turns
orange when the footprint exceeds it 4×, since that gap is itself the compression signal.

**Network and disk throughput added.** `sysctl NET_RT_IFLIST2` was implemented, debugged and then
abandoned on evidence: scanning every offset of the 180-byte `en0` message found no field holding the
true 39.5 GB lifetime total, and the plausible offsets carry only its low 32 bits — the failure mode
is a rate that silently breaks past 4 GB. `netstat -ibn` parsed from the `<Link#N>` row gives the
full counters. Disk uses `iostat -Id` cumulative totals, so no blocking sample window is needed. Both
are counter deltas sampled at the *end* of a frame rather than inside the thread pool, because
measured across thread submission the window is milliseconds and reads as a flat zero; both report
unprimed rather than a fabricated 0/s on their first reading.

**Four new modes**: `--space` (57 GB reclaimable found across 72 locations here — `.next` 8.2 GB,
go-build cache 5.2 GB, Docker 11.6 GB, bun cache 2.2 GB; ~28 s, hence its own mode rather than a
dashboard section), `--orphans` (6 stuck Playwright sessions, 33 processes, 940 MB), `--only
<section>`, and `--history`. The orphan detector's first version matched `--user-data-dir` alone and
returned 50 hits including Anytype and Docker Desktop — every Electron app passes that flag — so it
now additionally requires a temp-directory profile or a framework marker, verified with negative
controls in both directions.

**History** is a SQLite ring buffer at `~/.local/state/machine-monitor/history.db` with 14-day
retention, braille sparklines (two data points per cell at four levels), and named snapshots with a
diff. Building it surfaced a bug of exactly the class this session has been hunting: with the thermal
section disabled, a missing temperature was recorded as `0.0` and the trend then read "CPU temp 0 °C,
down from 40" — a fabricated reading indistinguishable from a cold machine. Metrics are now stored as
NULL when not collected, series and trends filter them, and `COUNT(metric)` replaced `COUNT(*)` so
uncollected runs do not inflate the sample count.

**Interactive filter and sort** in live mode. The first implementation drained the input queue and
returned only its last character, which silently ate the `/` of a "slash then text" sequence because
a frame takes long enough to gather that both arrive in one read — sort worked, filter appeared
broken. Keys are now queued and served in order. Proven through a pty: `m` produced "sorted by
memory", `/node⏎` left 29 of 29 process rows containing "node".

**Destructive actions** live in `scripts/actions.py`, kept separate so the whole destructive surface
is one readable file. Contract: a target comes from a candidate list, is re-checked immediately
before the signal (catching a recycled pid — verified), and nothing is signalled without
`confirm=True`. SIGTERM then SIGKILL after a grace period. Testing found a real defect: `os.kill(pid,
0)` cannot distinguish a live process from a zombie, so a successful kill reported "survived
SIGKILL"; `_alive` now checks process state and treats `Z` as dead. Six cases pass — obedient
process, SIGTERM-ignoring process escalated to SIGKILL in 1.4 s, unconfirmed refusal, dry run,
already-dead, and batch. Guards refuse pid 0/1, self, parent, and WindowServer. End-to-end proof:
two decoy servers created, one terminated, the other still listening. `--reclaim` and
`--kill-orphans` require the typed word "yes" and refuse outright without a tty. Interactive kill was
built as a typed pid rather than an arrow-key row cursor — a frame takes ~2.7 s, so a highlighted row
would be seconds stale when acted on — with the confirmation naming the process, its memory, uptime
and ports; driven through a pty against a decoy that did in fact die.

**Background alerting** uses launchd as the scheduler with each run a one-shot `--check`, rather than
a resident daemon that could wedge or outlive its config. Notification, one-hour cooldown, and
clearing a condition's record once it clears were all verified; the install/status/uninstall cycle is
clean and **the agent was left uninstalled** — leaving a background process running is the user's
call, not the agent's.

**`tests/eval_scenarios.py`** renders ten synthetic machines (hot, memory full, swap thrashing, disk
full, CPU pinned, no sensors, no memory source, empty, absurd values, healthy) and asserts on
colours, alerts, empty states and line widths. 10/10 pass. The first run reported a failure that was
the *test* being wrong — a bare `"MEM " in plain` matched two table headings — now matched by row
shape. Crucially the suite carries a negative control: breaking `temp_crit` makes the "hot" scenario
fail with the right message, proving it can detect a regression rather than always reporting green.

Docs restructured per the `skill-development` guidance: SKILL.md is 701 words and points at
`references/metrics.md` (what each metric measures and the wrong readings to avoid) and
`references/troubleshooting.md` (what every blank section means). Final verification: all six modules
compile, every CLI mode returns a correct exit code checked without a pipe (a trailing `| tail` would
have reported `tail`'s status), zero overflowing lines at 80/100/120/160/200 columns, 10/10
scenarios. Nothing committed or pushed — public repo, the user's call.

---

## Session — 2026-08-09 — v2.0.0 (bash → Python rewrite, real °C, honest memory)

Rewrote the whole tool from a 401-line bash script into zero-dependency Python modules, driven by the
observation that ~85% of the bash script was parsing CLI output — the exact work bash is worst at —
while only the ANSI rendering genuinely suited it. Four research passes ran first: Apple Silicon
temperature sensors, a survey of btop/htop/macmon/glances/Stats for what to steal, the Python stack
question (stdlib vs psutil), and a line-by-line defect audit of the bash.

The headline finding overturned a claim the tool itself published: **Apple Silicon does expose real
die temperature**. No public API does — `powermetrics` gives thermal pressure only and its `smc`
sampler does not exist on arm64, which is what the old claim was actually based on — but the private
`IOHIDEventSystemClient` HID sensor services do, with no sudo and no install, and that is what
Stats.app/btop/macmon read. Bound through `ctypes`; verified live at CPU die 38.5–41.7 °C, GPU
40.2–44.4 °C, battery 31.4 °C, SSD 33.0 °C. `PMU tcal` is filtered out explicitly: it is a
calibration constant that never moves and pulls the average several degrees off.

The most consequential bug fixed was the memory readout. The bash header hardcoded a 4096-byte page
size; Apple Silicon uses 16384, so it under-reported by 4x — and it also omitted compressed memory
entirely and never mentioned swap. On the development machine it printed `mem: 1.5G / 16G` at a
moment when the true figure (wired + active − purgeable + compressed, htop's definition) was 12.3 GB
and swap was 88% full. The tool was reporting a comfortable machine while it thrashed. Page size is
now read from `vm_stat`'s own header, never assumed.

Second-most consequential: `TOP CPU` ranked by `ps aux -r`'s `%CPU`, which is a lifetime average
since process start — for a dev server up four days it barely moves. Replaced with `ProcessSampler`,
which differences each process's cumulative CPU time over a real wall-clock window. Per-core P/E load
added via `host_processor_info` through ctypes, the one other thing no CLI exposes.

Other confirmed bash defects closed: `bar()` used `seq 1 $filled`, and BSD `seq` counts *down* when
first > last, so `seq 1 0` emits "1 0" — a 0% bar rendered 2 filled cells inside a 22-cell bar that
was supposed to be 20. `56*)` was a glob, so ports 5601 and 5678 were labeled "rapportd". Port 3009
appeared in two `case` branches, the second dead. `%-22b` padded on ANSI *byte* count, so every
column after a colored cell was misaligned. `lsof | awk '/LISTEN/'` structurally cannot see UDP
sockets (70 were live and invisible) and its `sort -t: -k2 -n` sort key breaks on IPv6 `[::1]:5432`.
`awk '{print $1}'` on the command string cut executable paths at the first space, so six distinct
Chrome-family processes all rendered as the identical label "Google" — 292 of 760 running processes
have a space in their path. `grep -c ... || echo 1` captures both grep's "0" and echo's "1", giving
`0\n1` and a live bash syntax error the moment the display count query misses.

Rendering was rebuilt around a `Column`/`Canvas.table` mechanism where cells are supplied as
`(plain_text, color)` so truncation always happens on real text — colorizing first and cutting after
can slice an escape sequence in half — and where a table that cannot fit drops its lowest-priority
columns instead of overflowing. Color is gated on `NO_COLOR`/`TERM`/`COLORTERM`, never `isatty()`,
which is False under an agent harness while the terminal renders color fine.

Verified: full render at COLUMNS 80/100/120/160/200 with zero overflowing lines; all server and
process rows measured to identical visible length, proving the padding fix; bars measured at exactly
22/28 cells with 0% rendering 0 filled; `NO_COLOR=1` emits 0 escape sequences. Driving the
interactive `-d` loop through a real pty caught a bug no non-interactive test could have: `q` did not
quit, because cbreak was entered only for the duration of `read_key`, so any key pressed during the
~2 s a frame takes to gather was held by the line discipline in canonical mode. The terminal is now
held in cbreak for the whole session and keys are read with `os.read`. Frame time went from 3.54 s
(~568 subprocess forks) to ~2.0 s by running the independent subprocess collectors concurrently.

`scripts/monitor.sh` was deleted rather than kept as a shim.
