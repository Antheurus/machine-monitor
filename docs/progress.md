# machine-monitor Progress

## Session — 2026-09-30 (cont) — v2.5.1 (--space dropped any directory du could not fully read)

A disk cleanup on the live machine found `~/Library/Caches/go-build` at 27.2 GB by a hand `du` pass,
while `--space` had not listed it at all even though it is in the scanner's cache list. Root cause is
the same class as the `cwd_for_pids` fix earlier today: `_dir_size` went through `run()`, which
returns '' on any non-zero exit, and `du` exits 1 on a single unreadable entry while still printing
a valid total — so the size read as 0, fell under the 50 MB floor, and the biggest item on the disk
vanished from the report. Reproduced on a 58 MB directory holding one `chmod 000` child (du rc=1,
`_dir_size` -> 0). `_dir_size` now reads stdout regardless of exit code, with the timeout raised
90 -> 240s for trees of millions of small files. `tests/test_space_sizes.py` proves it and was
falsified against the pre-fix `collect.py` (stashed): FAIL 0 bytes there, PASS 8388608 after. This
is the third time in the codebase `run()` has swallowed a valid lsof/du answer (after `owner_app`
and `cwd_for_pids`), so its docstring now says never to use it for either; the remaining callers
were swept — `clones.py` and `owner_app` already read stdout directly, and `listeners()` exits 0
(an empty result there is a genuine no-match). The cleanup itself: `go clean -cache`, `bun pm cache
rm`, `pnpm store prune` and `brew cleanup -s` took free space 46.64 -> 77.61 GB by statvfs;
`npm cache clean` refused on root-owned files and needs `sudo chown` from the user. All suites
green: space_sizes, detached_stacks, orphan_naming, grind_and_sessions 20/0, clones 27/0,
eval_scenarios 11/11.

---

## Session — 2026-09-30 — v2.5.0 (portless dev stacks, agent-session memory, a stranded fix landed)

Asked to fold what had been learned in the field back into the skill. The gap list came from the
memory notes of the "panas nih" sessions, not from the code: the biggest RAM finding of 2026-09-11
(six `just dev-be` copies of one project, `pnpm → nest watch → dist/main`, 7-9 days old, ~4.5 GB,
zero listening sockets) was structurally invisible to the tool, because SERVERS RUNNING, the
stale-server alert and `--reclaim` are all built from the listener table. New
`collect.detached_stacks` finds trees whose root is a dev runner by `comm` basename (configurable
`[sessions] dev_runners`), was adopted by launchd (ppid 1), sits under a project root, is older than
`stale_server_hours`, and holds no listener anywhere in the tree; memory is the whole tree's
footprint. It feeds a NEEDS ATTENTION warn, the `--json` payload (`detached_stacks`) and a new
DETACHED DEV STACKS table under `--reclaim`, which now signals every pid in the tree (supervisor
included — killing a watcher's child only gets it respawned) with each pid's `lstart` re-checked by
`refuse_reason`. Second addition: `collect.agent_sessions` counts outermost `claude`/`codex`/`cursor`/
`windsurf` sessions with their descendants (the MCP servers are what closing a tab gives back) and
reports at `agent_sessions_warn = 6` — report-only, since which tabs to close is the user's call.
Building this surfaced two real bugs. `cwd_for_pids` went through `run()`, which returns '' on any
non-zero exit, and `lsof` exits 1 whenever one pid in the batch is unreadable — so a single
root-owned pid blanked every cwd; it is latent in the listener path today (non-root lsof only lists
user-owned listeners) but the stack finder passes arbitrary launchd children, so it would have found
nothing. Falsified: the old `run()` path returns '' for `[1, own pid]`, the new one resolves own pid.
And the first cut of the finder took the runner name from argv split on a space, the same defect the
stranded orphan fix below corrects — the new test's `Google Chrome` configurable-runner case failed
on it, so both now read `comm`. Also landed that stranded work: uncommitted since 2026-08-16/20 and
not included in the v2.4.0 commit, `orphan_automation` took `binary` from argv split on a space
(so `--kill-orphans` refused its own targets for any Chrome helper path) and now passes the profile
as argv mark; `tests/test_orphan_naming.py` covers it, plus the two reference sections linked from
`gotcha-coding.md` E3/E6. Smaller: `_g` printed 47 MB as `0.0G` in the stale-server alert (now
`M` below 1 GiB), and the footer's kill hints said `kill -9`, contradicting the SIGTERM-first
contract. SKILL.md gains a "Where the memory actually is" section (measured ranking: orphaned
browsers 5.8 GB, portless stacks 4.5 GB, agent sessions 5.28 GB, stale servers 81 MB — run
`--orphans` before `--reclaim`), power draw instead of °C as the before/after heat figure
(16,162 → 1,094 mW on 2026-08-26), and "a graceful quit under thrash takes minutes". Verified:
`test_detached_stacks` all pass (finder + five lookalikes it must let through + configurable runner +
agent count/threshold both sides + cwd with pid 1), `test_orphan_naming` pass, `test_grind_and_sessions`
20/0, `test_clones` 27/0, `eval_scenarios` 11/11; live `--reclaim --dry-run` renders both tables and
correctly excludes `just fe` (pid 6341, ppid 1) because its `node` child listens on :3512. The live
machine had zero portless stacks today, so the positive path is proven only against the synthetic
tree. Follow-up worth considering: `--reclaim` on a listening server still kills only the listener,
leaving its `just`/`pnpm` supervisor alive.

---

## Session — 2026-08-31 — v2.4.0 (a demo GIF for the README, generated rather than recorded)

The README had no image, so the ask was a GIF of the dashboard for the repo front page. The first
attempt was the obvious one — screen-record the real dashboard and cut 00:16–01:16 out of the `.mov`
with ffmpeg — and it produced a perfectly good 8.55 MB GIF that must not be published. This repo is
PUBLIC, and the real dashboard's whole selling point is that it maps every listener to the project
directory it came from, so the frame carried nine client/project names out of `SERVERS RUNNING`,
six more out of the Docker container list, the automation session names from `--sessions`, and the
operator's home path in the footer. Cropping cannot fix it: the project column *is* the feature
being demonstrated. The user chose to re-shoot against fictional data rather than blur it.

`tests/demo_render.py` builds a plausible machine and hands it to the real renderer via
`app.build(snap, cfg, Theme(True), "once")` — the same entry point `tests/eval_scenarios.py` already
used, which is what made this cheap. The shipped scenarios were the obvious thing to reuse and were
wrong for it: they carry one listener and processes named `proc0`–`proc13`, which renders a sparse
dashboard that reads as broken rather than as a demo. So the demo snapshot is its own thing — twelve
listeners, six containers, twelve processes, sized so every section lights up including the two that
only appear under load (`NEEDS ATTENTION`, and `WINDOWSERVER DIAGNOSIS`, which is gated at 15% CPU).
Names follow the seed-data rule: `acme-storefront`, `northwind-admin`, `orbit-dashboard`,
`atlas-scraper` — real-shaped, real-length, not `TEST_PROJECT_1`. Three things had to be neutralised
beyond the project column, and two of them were only found by looking at a rendered frame:
the Docker container names, the `--sessions` labels, and `config_mod.CONFIG_PATH`, which the footer
prints verbatim and which carried `/Users/macbook/` into all 45 frames.

A static snapshot makes a dead GIF, so `snapshot()` takes a `tick` and animates only what a real
machine moves between samples — core load, per-process CPU, throughput, die temperature, thermal
power. Ports, ages and container names stay put, because those are what a viewer is actually
reading. The payoff was unplanned: because TOP CPU sorts on the animated figure, the rows re-order
across frames on their own, and WindowServer visibly climbs the table.

`tests/ansi_to_gif.py` renders the frames to PNG with PIL and Menlo and stitches the GIF directly,
so no screen recording is involved at all — no window chrome, no cursor, no capture noise on the
text, and it is reproducible. It implements only the SGR codes this dashboard emits (0, 1, 2, and
`38;5;N`) plus the xterm-256 palette; `--strict` turns an unhandled code into a hard failure so a
future renderer change cannot silently degrade the image rather than announcing itself. Verified by
reading the artifact back rather than trusting the encoder: 45 frames all distinct after
quantisation, and two extracted frames inspected by eye — which is how the footer path leak and the
`⚠` glyph question were settled (Menlo does carry U+26A0; it is just squat at 13px, so it matches
what the real terminal shows). Output is 992x1814, 45 frames at 6 fps, 6.52 MB, under GitHub's
practical README ceiling.

Left alone deliberately: `scripts/collect.py`, `scripts/main.py`, `references/metrics.md`,
`references/troubleshooting.md` and `tests/test_orphan_naming.py` were already modified in the
working tree at session start and belong to another session, so staging was path-scoped to the four
files this session wrote. All three existing suites re-run green afterwards — 11/11 scenarios, 27
clone tests, 20 grind/session tests — and `scripts/` was never touched, so that was a regression
check on the shared renderer, not a formality.

## Session — 2026-08-10 (cont) — v2.3.0 (stuck background work, and a kill guard for live agent sessions)

Both changes came out of a live "panas nih coba cek" diagnosis rather than a feature request, and
both are things the dashboard could not previously see. The heat had no thermal explanation at all —
CPU die 50.1 C, GPU 52.9 C, pressure Nominal — while `kernel_task` sat at 20.8% and the chassis was
warm. The cause was three storage daemons (`StorageManagementService`, `ApplicationsStorageExtension`,
`Storage.appex`) grinding at a combined ~140% for 7h48m because a System Settings > Storage pane had
been left open during an earlier System Data investigation. They have no window, no port and little
memory, so every existing section rendered them as unremarkable. Fix is `collect.grinding()` plus a
`Grinder` dataclass, feeding `attention()`. The discriminator is requiring the live CPU delta AND a
new `Process.lifetime_cpu_pct` (cumulative CPU time over wall age) to be high at once: live alone
flags every compile, lifetime alone flags a process that worked hard early and went quiet. That
second figure is exactly what `ps %CPU` reports, which `references/metrics.md` had (correctly)
written off as decorative for "what is busy now" — the reference now carries both readings instead of
one, since it is the right answer to a different question. `ProcessSampler` already parsed cumulative
CPU time and was discarding it, so the collector change was one field. Calibration on this machine:
the stuck daemon sat at 34% sustained while WindowServer (13.8%) and Docker's VM host (11.5%) fall
just under the 15% default, a >2x separation. `KNOWN_GRINDERS` maps a handful of windowless daemons
(Storage pane, Spotlight, Photos analysis, iCloud, Time Machine, content caching) to a cause and a
remedy; anything unrecognised is still reported, with its pid and no invented explanation.

The second change is a genuine safety bug found while cleaning up: `--reclaim`'s candidate set was
`is_project and age >= stale_hours` with `terminate_all` called with neither an argv mark nor an
`lstart`, and a dry run proved it would have terminated `plannotator` — a helper of a `claude` session
that had been running four days. An agent CLI started inside a repo leaves helpers that pass every
test a dev-server sweep applies: right cwd, listening port, old. New `collect.mark_session_owned()`
walks each listener's ppid chain and marks anything descended from a live `claude`/`codex`/`cursor`/
`windsurf` (configurable via a new `[sessions] owners`), reusing the existing `_ps_tree()` rather than
adding a second ancestry primitive. Ancestry, never the process's own argv — `plannotator` is spelled
like an ordinary project binary and only its parent reveals what it is, the same reason `sessions()`
groups by ancestry. The guard is deliberately narrow: a leftover whose session exited reparents to
launchd, so the chain ends at pid 1 and a genuine stale server stays claimable. `attention()` excludes
owned listeners from the stale count too, and `--reclaim` prints each spared target with its reason
rather than filtering silently. `config.py` only sets `session_owners` when the user wrote a non-empty
list, so a missing section cannot read as "no owners" and disable the guard.

Two artifacts from the same session were deliberately NOT folded in: an ad-hoc kill loop (weaker than
the existing `actions.py`, which already has the `lstart` reuse guard, zombie-aware `_alive` and
`PROTECTED_NAMES`) and an ad-hoc listener classifier (`is_project` already excludes the Warp/OneDrive
shapes it was written for; only the ancestry half was new). Adding either would have been a second
divergent implementation of something that already has an owner.

Verified: `tests/test_grind_and_sessions.py` added, 20 assertions, every catch paired with the
lookalike it must let through (fresh spike, old-but-idle, old-but-bursty, launchd-reparented orphan,
a binary merely *containing* `claude`, a cyclic parent chain, an empty owner list). 20 passed / 0
failed; `test_clones.py` 27 passed, `eval_scenarios.py` 11/11 — no regression. `--reclaim --dry-run`
against the live machine went from "1 process would be terminated" to "0 targets, 1 spared" naming
the claude session. End-to-end proof of the detector used two real spawned processes at shipped
thresholds (only `grind_min_hours` relaxed, since a test cannot wait two hours): the busy one
measured 99.0% live / 100.2% lifetime and produced a real alert, the sleeping sibling measured 0/0 and
did not. Reopening the Settings pane to reproduce the original daemon did not restart the scan, so
that path is unproven this session and is noted rather than claimed. Files touched: `scripts/collect.py`,
`scripts/main.py`, `scripts/config.py`, `scripts/config.ini`, `SKILL.md`, `references/metrics.md`,
`tests/test_grind_and_sessions.py`. Follow-up worth considering: `KNOWN_GRINDERS` is a code-level
table and would be better as config once a second machine disagrees about what counts as a culprit.

---

## Session — 2026-08-10 (cont) — v2.2.0 (code-sign clone litter, and apparent size stops being quoted as disk)

Came out of a live diagnosis, not a feature request: macOS Settings reported **System Data 180.52 GB**
on a Mac with 79 GB free, and `du -sh -x /private/var/folders` answered **609 G on a volume holding
375 GB**. That impossibility was the whole diagnosis — the cause was 258 leftover
`com.google.Chrome.code_sign_clone` copies of `Google Chrome.app` under
`/private/var/folders/jz/…/X/`, 2.21 GB each, 652 GB apparent, accruing ~32/day since 2026-08-02.
Deleting all 258 freed **2.4 GB**: they are APFS clones sharing blocks with the original, and `du`,
the Storage pane and this tool's own `--space` all bill them at full size. The 260x gap between the
apparent and real figures is the thing the release encodes.

New `scripts/clones.py` finds the buckets (`/private/var/folders/*/*/X/*.code_sign_clone`), sizes them
the way `du` does so the litter is *findable*, and reports that number explicitly as an upper bound.
The reclaim figure is never derived from it — `purge()` reads `statvfs` immediately either side of the
removal and reports what the volume actually gave back. `lsof -n -Fn +D` names the clones a process
currently holds open, so a cleanup runs without quitting the app (2 of 258 were live here); when
`lsof` cannot answer, `in_use` is `None` and the bucket is **refused rather than guessed at** —
distinct from an empty set, and conflating the two would delete a live clone.

Deletion went into `actions.py` rather than beside the detector, keeping the promise in its module
docstring that the destructive surface is one small readable file. `remove_dirs()` takes an explicit
list — there is nowhere to pass a `*` or `.` pathspec — and `refuse_removal()` re-resolves both sides
with `realpath` before comparing, since a symlinked parent otherwise lets a target that reads as
inside the base resolve anywhere; the separator is part of the prefix test so `/tmp/safe-evil` cannot
pass for a base of `/tmp/safe`. A `PROTECTED_BASES` set rejects a base as shallow as
`/private/var/folders`. Two things were also fixed while here: `draw_space` promised a specific
post-cleanup disk percentage from `du` sums and now says "no better than", and
`confirm_destructive()` hardcoded "process(es)" in its own prompt while taking a `what` verb, so it
gained a `noun` parameter instead of being forked.

Verified three ways. `tests/test_clones.py` (new) is 27 checks, with every guard proved in both
directions — the allow case matters as much as the refusal, since a check that only ever refuses is
indistinguishable from a broken one. It shipped flaky on the first pass and the flake was the test's
fault, not the code's: it asserted that `real_bytes` and `apparent_bytes` merely *differ*, and
deleting a few kilobytes of ordinary files can move free space by exactly the apparent amount, so it
failed once in about eight runs for a reason unrelated to what it was checking. Replaced with a
deterministic pair — the measured delta must equal `free_after - free_before`, and a hand-built
`PurgeReport` with a 1 TB apparent size against a 50-byte delta proves the apparent figure never
leaks into the real one. 20 consecutive runs, 27/27 each. `tests/eval_scenarios.py` stayed 11/11 plus unit
checks after the `draw_space` and `confirm_destructive` edits. And the full CLI was driven at the real
path: a synthetic 6-clone bucket built under the live `/private/var/folders/jz/…/X/`, `--clones`
found it, `--clean-clones --dry-run` planned 6 and deleted 0, `--clean-clones --yes` removed 6 and
reported `5.7M apparent → 5.8M actually freed`. That last pair is the positive control — for genuine
files the two figures agree, which proves the `statvfs` measurement is live rather than cosmetic, and
for real clones they diverge by 260x.

Follow-up not done: nothing schedules this, so the litter regrows at ~32/day. A `--watch`-style
periodic sweep is the obvious next step and was deliberately left out of this change.

---

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
