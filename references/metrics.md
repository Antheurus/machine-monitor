# What each metric actually measures

Written because the obvious source for most of these is wrong, and wrong in a direction that reads as
plausible. Every figure below was measured on an Apple Silicon MacBook Pro (M2 Pro, 16 GB) while
building the tool.

## Memory per process — `MEM` supersedes `RSS`

`MEM` is **physical footprint**, the number Activity Monitor shows. `RSS` is resident set size and is
shown beside it, dimmed, and turns orange when it falls far below the footprint.

Ranking by RSS is not merely imprecise, it is structurally unable to find the largest consumers on a
machine under memory pressure: once macOS compresses a process's pages they stop being resident, so
its RSS collapses while its real cost does not. Measured simultaneously:

| Process | RSS | Footprint | Ratio |
|---|---|---|---|
| `node` | 3 MB | 2269 MB | 681× |
| `next-server` | 9 MB | 2076 MB | 231× |
| `Maccy` | 30 MB | 1982 MB | 65× |
| `com.apple.Virtualization.VirtualMachine` | 838 MB | 4105 MB | 5× |

The footprint comes from `top -l 1 -stats pid,mem`. There is no cheaper route: `ps` has no footprint
column, and `proc_pid_rusage` via ctypes returns EPERM for any process the caller does not own — 59
of 60 sampled — because it needs a private codesign entitlement that only Apple's own binaries carry.

RSS still has one honest use: a footprint far above it is the signal that the process has been
compressed or swapped, which is exactly what the orange highlight marks.

## Process name — a framework helper names no app

`com.apple.Virtualization.VirtualMachine` is the XPC service Apple's Virtualization.framework spawns
on behalf of *whichever* app asked for a VM: Docker Desktop, UTM in its Apple-VZ mode, Podman,
VirtualBuddy. The executable name identifies the framework, and launchd reparents the service so its
`ppid` is 1 — so both of the usual attribution routes are dead, while the row itself sits at the top
of the RAM table at 4 GB. A reader therefore names whichever VM app they remember installing, and on
this machine that produced a confident, wrong "UTM is your biggest hog" for an app that had already
been uninstalled. It was Docker Desktop.

The tool resolves the owner from the files the helper holds open — `lsof -p <pid> -Fn`, then the most
frequently referenced `*.app` bundle outside `/System`, which for this process is
`/Applications/Docker.app/Contents/Resources/linuxkit/kernel` and its `Docker.raw` disk image. Most
frequent rather than first-seen, because a helper incidentally reads a font or resource out of some
unrelated bundle. Rendered as `Docker · com.apple.Virtualization.VirtualMachine`, owner first so the
attribution survives the column truncating the tail.

Two constraints on any change here. `Process.name` stays the raw `comm` and the owner lives in its
own field: `actions.refuse_reason` re-reads `ps comm=` and compares it against the caller's copy to
catch pid reuse, so a decorated name makes every kill of a resolved helper refuse itself with
`pid now belongs to com.apple.Virtualization.VirtualMachine, not Docker · com.apple.…`. And `lsof`
exits non-zero whenever any single fd is unreadable, so its stdout is read regardless of return code
rather than through `run()`, which returns `''` on a non-zero exit.

## CPU per process — two different figures, both real

`ps aux`'s `%CPU` is an average over the entire life of the process. For a dev server running four
days it barely moves regardless of what it is doing right now, which makes a "top CPU" table built on
it decorative.

So the `CPU%` column differences each process's cumulative CPU time across a real wall-clock window
(`cpu_sample_seconds`, default 0.5 s) — the same arithmetic `top` performs internally, over an
interval we control and across every process rather than top's truncated list.

A multi-threaded process legitimately exceeds 100%: that is cores, not a bug.

**The lifetime average is not junk, though — it answers a different question, and it is the one
question no other metric here can answer.** `Process.lifetime_cpu_pct` (cumulative CPU time over wall
age, i.e. what `ps %CPU` reports) says whether a process has been grinding the *whole time it has
existed*. A momentary spike cannot move it, so pairing it with the live figure separates a daemon
stuck in a loop from an app that is briefly busy — which is exactly what `grinding()` does, requiring
both to be high at once. Neither figure alone works: live CPU alone flags every compile, and the
lifetime average alone flags a process that worked hard early and has since gone quiet.

Calibration measured on this machine: a stuck storage daemon sat at 34% sustained across 7h48m, while
WindowServer (13.8%) and a VM host process (11.5%) sit just under the 15% default — more than a 2x
separation between the pathological case and the busiest legitimate ones.

## System memory — the compressor is the missing term

`used = wired + (active − purgeable) + compressed`, at the page size read from `vm_stat`'s own header.

Two independent errors are common here and they compound:

1. **Hardcoding a 4096-byte page.** Apple Silicon uses 16384. The bash predecessor did this and
   under-reported by 4×.
2. **Omitting the compressor.** On a loaded machine it is the largest single category.

Together they produced a header reading `mem: 1.5G / 16G` at a moment when the true figure was
12.3 GB with swap 88% full — a machine reported as comfortable while it was thrashing.

`inactive` is reclaimable and is reported separately, never folded into "used". `top` folds it in,
which is why `top` shows a larger number than this tool; both are defensible, and this one matches
Activity Monitor's "Memory Used".

**Always read `SWAP` beside `MEM`.** Memory can look moderate while swap is exhausted.

## Temperature — real, via a private API

CPU die, GPU, battery and SSD temperatures are read through `IOHIDEventSystemClient` in `ctypes`. No
root, no install.

The widespread claim that Apple Silicon exposes no die temperature is false, but understandable:
`powermetrics` reports thermal *pressure* only, and its `smc` sampler does not exist on arm64
(`unrecognized sampler: smc`), so testing that one route concludes the sensor is absent. The HID
sensor services are what Stats.app, btop and macmon all read.

**`PMU tcal` is excluded deliberately.** It looks like a sensor and is a calibration constant — a
fixed 51.85 °C here — and averaging it into the die figure drags it several degrees.

Sensor names are grouped: `PMU tdie*`/`tdev*`/`TP*s` → CPU, `TP*g` → GPU, `gas gauge` → battery,
`NAND` → SSD. On M2 the clean `eACC`/`pACC` prefixes that M1 had are absent, so the CPU/GPU split is
by naming convention rather than a documented map.

## Per-core load

From `host_processor_info` via ctypes — no CLI exposes per-core ticks. Values are a delta between
frames.

**Apple Silicon enumerates efficiency cores first**, so on a 6P+4E machine cores 0–3 are E and 4–9
are P. The legend under the sparkline states the split.

## Network and disk throughput

Network is `netstat -ibn`, parsed from the `<Link#N>` row that carries interface totals. The kernel
route-table route (`sysctl NET_RT_IFLIST2`) was tried and abandoned: scanning every offset of the
180-byte `en0` message found no field holding the true 39.5 GB lifetime total, and the plausible
offsets carry only its low 32 bits — so any rate built on them silently breaks past 4 GB.

Disk is `iostat -Id`, whose `-I` gives cumulative totals rather than a windowed average, so no
blocking sample interval is needed. It reports one combined figure, which lands in the read slot; the
write slot stays zero rather than being invented.

Both are counter deltas and report `primed=False` on the very first reading rather than a fabricated
0/s. Loopback is excluded from the "busiest interface" pick.

## Listeners

`lsof -nP -F pn` field mode, TCP listeners and bound UDP sockets. The columnar output is not used: it
breaks on IPv6 (`[::1]:5432`), and filtering on the literal string `LISTEN` structurally cannot see
UDP — 70 sockets were live and invisible in the predecessor.

The project column is `lsof -a -d cwd` per pid, in one call. It is what distinguishes two identical
`next-server` processes — which worktree each was started from — and it drives the duplicate-server
and stale-server warnings. A cwd of `/` or under `~/Library` is not a project and is excluded from
those checks; without that filter, Anytype alone contributed eight phantom "dev servers".

## What is genuinely unavailable

- **CPU frequency.** `sysctl(HW_CPU_FREQ)` fails on Apple Silicon.
- **Per-process GPU utilisation.** No API. Only a system-wide IOKit figure exists.
- **ANE utilisation.** Every tool that displays one is computing it from assumed maximum power. Do
  not add it.
- **Per-process disk I/O.** Not implemented in any accessible source short of undocumented
  `rusage_info` territory.

## Orphaned automation browsers

Matched on `--user-data-dir` pointing at a temp directory or a path naming a framework
(playwright, puppeteer, selenium, chromedriver, chromiumdev). Matching the flag alone is wrong:
every Electron app passes it, which reported Anytype and Docker Desktop as orphans — 50 hits of
which 1 was real.

The needle is assembled at runtime and the tool's own pid and parent are excluded, because a probe
that searches for a literal it also contains finds itself.

## macOS metric probes that lie — the four readings and their fixes

_linked from ../rules/gotcha-coding.md of E3_

**(a) Die temperature.** Apple Silicon DOES have a readable die temperature, and the belief that it does not is the confident kind of wrong because the usual probe genuinely fails: `powermetrics` reports thermal *pressure* only and its `smc` sampler does not exist on arm64, so anyone testing that route concludes the sensor is absent. The private `IOHIDEventSystemClient` HID services expose it — no sudo, no install, ~90 lines of `ctypes`. Filter `PMU tcal` out of any average: it is a calibration constant that never moves and drags the die figure several degrees.

**(b) CPU.** `ps aux`'s `%CPU` is a lifetime average since process start, not current load, so a dev server pinning a core for days reads near zero and a "top CPU" table built on it is decorative. Difference each process's cumulative CPU time over a wall-clock window: `ps -eo pid=,time=` twice, ~0.5s apart.

**(c) Memory.** `vm_stat`'s page size is 16384 on Apple Silicon, not 4096, and "used = active + wired" omits the compressor, which is the largest category on a loaded machine — the naive formula reports a comfortable machine that is thrashing. Read the page size from `vm_stat`'s own header line, use htop's `wired + active − purgeable + compressed`, and always show `sysctl vm.swapusage` beside it.

**(d) Ports.** `lsof | awk '/LISTEN/'` structurally cannot see UDP, and its columnar output breaks on IPv6 `[::1]:5432`. Use `lsof -nP -F pn` field mode and parse it.

**Two rendering traps in the same family.** BSD `seq` counts DOWN when first > last, so the near-universal bar idiom `printf '█%.0s' $(seq 1 $filled)` draws 2 filled cells at 0%. And `printf '%-22b'` pads on ANSI byte count, so pad on visible width with the escapes stripped and truncate the plain text *before* colorizing, never after.
