#!/usr/bin/env python3
"""Stuck-daemon detection and the live-session guard, proved in both directions.

Both checks exist to say "no" to something, and a check that only ever says no is
indistinguishable from a broken one — so every case that must be caught is paired
with a lookalike that must be let through. The lookalikes are the point: they are
the shapes that make a naive version of each check wrong.

Run directly:

    python3 tests/test_grind_and_sessions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import collect  # noqa: E402

PASSED = FAILED = 0

CFG = {"thresholds": {"grind_cpu_pct": 25, "grind_lifetime_pct": 15, "grind_min_hours": 2}}


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok    {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def proc(pid: int, name: str, cpu: float, age_hours: float, cpu_minutes: float) -> collect.Process:
    return collect.Process(
        pid=pid, name=name, rss=10 << 20, cpu=cpu,
        age_seconds=age_hours * 3600, cpu_seconds=cpu_minutes * 60,
    )


def test_grinding() -> None:
    print("\ngrinding() — busy NOW and busy ALL ALONG, both required")

    stuck = proc(99753, "ApplicationsStorageExtension", cpu=59.5,
                 age_hours=7.8, cpu_minutes=160.8)
    # Busy right now, but only just started — a compile, a video export.
    spike = proc(200, "cc", cpu=95.0, age_hours=0.2, cpu_minutes=11.0)
    # Old and has burned real CPU, but is idle now — a dev server that built once.
    settled = proc(201, "next-server", cpu=0.4, age_hours=90.0, cpu_minutes=600.0)
    # Old and busy now, but has idled most of its life — a VM host process.
    bursty = proc(202, "com.apple.Virtualization.VirtualMachine", cpu=39.7,
                  age_hours=131.0, cpu_minutes=300.0)

    found = collect.grinding([stuck, spike, settled, bursty], CFG)
    names = {g.name for g in found}

    check("catches the stuck storage daemon", "ApplicationsStorageExtension" in names,
          f"got {sorted(names)}")
    check("ignores a fresh spike (young, so no lifetime average yet)", "cc" not in names)
    check("ignores an old process that is idle now", "next-server" not in names)
    check("ignores an old process that idled most of its life",
          "com.apple.Virtualization.VirtualMachine" not in names,
          f"lifetime {bursty.lifetime_cpu_pct:.1f}%")

    hit = next(g for g in found if g.name == "ApplicationsStorageExtension")
    check("reports the sustained average, not just the live figure",
          33.0 <= hit.lifetime_pct <= 35.0, f"got {hit.lifetime_pct:.1f}%")
    check("names the cause for a known daemon", "Storage pane" in hit.cause, hit.cause)
    check("gives a remedy for a known daemon", bool(hit.remedy))

    unknown = proc(203, "somevendorhelper", cpu=80.0, age_hours=9.0, cpu_minutes=300.0)
    generic = collect.grinding([unknown], CFG)
    check("still reports an unknown daemon", len(generic) == 1)
    check("leaves cause empty rather than inventing one",
          generic[0].cause == "" and generic[0].remedy == "")

    newborn = collect.Process(pid=204, name="x", rss=0, cpu=99.0, age_seconds=0.0,
                              cpu_seconds=0.0)
    check("survives a zero-age process", newborn.lifetime_cpu_pct == 0.0)
    check("does not flag a zero-age process", collect.grinding([newborn], CFG) == [])


def test_session_guard() -> None:
    print("\nmark_session_owned() — ancestry, not the process's own argv")

    # pid -> (ppid, argv). The helper's own argv says nothing about claude, which
    # is exactly why matching each process on its own argv cannot work here.
    tree = {
        493: (1, "/Applications/Warp.app/Contents/MacOS/stable"),
        751: (493, "-zsh -g --no_rcs"),
        42184: (751, "claude --dangerously-skip-permissions"),
        94713: (42184, "plannotator"),
        # A genuine leftover: its session exited, so launchd reparented it.
        58575: (1, "next-server (v16.2.12)"),
        # A dev server under a plain shell — a human started this one.
        3066: (751, "node /Users/x/proj/node_modules/.bin/vite"),
    }
    cfg = {"session_owners": ["claude", "codex"]}

    helper = collect.Listener(port=61501, pid=94713, proto="TCP", name="plannotator")
    orphan = collect.Listener(port=3401, pid=58575, proto="TCP", name="next-server")
    human = collect.Listener(port=3066, pid=3066, proto="TCP", name="node")
    collect.mark_session_owned([helper, orphan, human], cfg, tree)

    check("spares a helper whose ANCESTOR is a live claude",
          helper.session_owner == "claude", f"got {helper.session_owner!r}")
    check("still claims a leftover reparented to launchd",
          orphan.session_owner == "", f"got {orphan.session_owner!r}")
    check("still claims a dev server started from a plain shell",
          human.session_owner == "", f"got {human.session_owner!r}")

    # "claude-proxy" is somebody's own binary, not an agent session.
    near = {10: (1, "/usr/local/bin/claude-proxy --port 9000"), 11: (10, "node worker.js")}
    lookalike = collect.Listener(port=9000, pid=11, proto="TCP", name="node")
    collect.mark_session_owned([lookalike], cfg, near)
    check("does not match a binary that merely contains an owner's name",
          lookalike.session_owner == "", f"got {lookalike.session_owner!r}")

    looped = {20: (21, "a"), 21: (20, "b")}
    cyclic = collect.Listener(port=1, pid=20, proto="TCP", name="a")
    collect.mark_session_owned([cyclic], cfg, looped)
    check("terminates on a cyclic parent chain", cyclic.session_owner == "")

    off = collect.Listener(port=61501, pid=94713, proto="TCP", name="plannotator")
    collect.mark_session_owned([off], {"session_owners": []}, tree)
    check("an empty owner list disables the guard rather than inverting it",
          off.session_owner == "")


def test_attention_excludes_owned() -> None:
    print("\nattention() — an owned helper is not counted as a stale dev server")

    mem = collect.Memory(available=False)
    disk = collect.Disk(total=0, free=0)
    old = 40 * 3600

    owned = collect.Listener(port=61501, pid=94713, proto="TCP", name="plannotator",
                             cwd="proj", age_seconds=old, is_project=True,
                             session_owner="claude")
    real = collect.Listener(port=3401, pid=58575, proto="TCP", name="next-server",
                            cwd="proj2", age_seconds=old, is_project=True)

    only_owned = collect.attention(mem, disk, [owned], [], {"thresholds": {}})
    check("no stale-server alert when the only candidate is session-owned",
          not any("dev server(s) up longer" in a.text for a in only_owned),
          str([a.text for a in only_owned]))

    with_real = collect.attention(mem, disk, [owned, real], [], {"thresholds": {}})
    stale_alerts = [a for a in with_real if "dev server(s) up longer" in a.text]
    check("still alerts on a genuine stale server", len(stale_alerts) == 1)
    check("counts only the genuine one",
          bool(stale_alerts) and "1 dev server(s)" in stale_alerts[0].text,
          stale_alerts[0].text if stale_alerts else "")


def main() -> int:
    test_grinding()
    test_session_guard()
    test_attention_excludes_owned()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
