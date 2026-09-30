"""Detached dev stacks, agent-session memory, and cwd lookup across foreign pids.

Every detection is paired with the lookalike it must let through: a stack that
still holds a port, one whose session is alive, one too young, one outside a
project, one that is not a dev runner. A check that only ever finds is as
useless as one that never does.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import collect  # noqa: E402

failures = []
DAY = 86400.0
HOME = os.path.expanduser("~")
PROJECT = f"{HOME}/Documents/acme-storefront"


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}\n        got {got!r}\n        want {want!r}")
    if not ok:
        failures.append(label)


def proc(pid, ppid, name, age=8 * DAY, mb=100):
    return collect.Process(pid=pid, name=name, rss=mb * 2**20, ppid=ppid,
                           age_seconds=age, footprint=mb * 2**20)


def machine(root_ppid=1, root_name="just", root_age=8 * DAY):
    """just -> pnpm -> nest watch -> node dist/main, the 2026-09-11 shape."""
    procs = [
        proc(500, root_ppid, root_name, age=root_age, mb=5),
        proc(501, 500, "pnpm", mb=80),
        proc(502, 501, "node", mb=300),
        proc(503, 502, "node", mb=900),
    ]
    tree = {p.pid: (p.ppid, f"/opt/homebrew/bin/{p.name} dev") for p in procs}
    return procs, tree


CFG = {"thresholds": {"stale_server_hours": 24}, "project_roots": [f"{HOME}/Documents"]}
STARTS = {500: "a", 501: "b", 502: "c", 503: "d"}


def find(procs, tree, listeners=frozenset(), cwd=PROJECT, cfg=CFG):
    return collect.detached_stacks(procs, cfg, set(listeners), tree=tree,
                                   cwds={500: cwd}, starts=STARTS)


print("\ndetached_stacks() — finds the portless leftover")
procs, tree = machine()
found = find(procs, tree)
check("one stack found", len(found), 1)
check("whole tree is the target", sorted(found[0].pids) if found else None, [500, 501, 502, 503])
check("memory is the tree's, not the root's", found[0].memory if found else 0, 1285 * 2**20)
check("each member carries its lstart", found[0].starts if found else None, STARTS)

print("\ndetached_stacks() — lets the lookalikes through")
check("a stack holding a port is a server, not this", len(find(procs, tree, {503})), 0)
procs2, tree2 = machine(root_ppid=4242)
check("a live session still owns it", len(find(procs2, tree2)), 0)
procs3, tree3 = machine(root_age=2 * 3600)
check("younger than the stale threshold", len(find(procs3, tree3)), 0)
check("cwd outside every project root", len(find(procs, tree, cwd=f"{HOME}/Library/x")), 0)
procs4, tree4 = machine(root_name="Google Chrome")
check("not a dev runner", len(find(procs4, tree4)), 0)
custom = dict(CFG, dev_runners=["Google Chrome"])
check("dev_runners is configurable", len(find(procs4, tree4, cfg=custom)), 1)

print("\nagent_sessions() — one row per outermost session, helpers included")
agents = [
    proc(10, 1, "Warp"),
    proc(20, 10, "claude", mb=400),
    proc(21, 20, "node", mb=800),        # its gitnexus mcp
    proc(22, 20, "claude", mb=300),      # a sub-agent: same session
    proc(30, 10, "claude", mb=400),
    proc(40, 10, "zsh", mb=5),           # not an agent
]
count, held = collect.agent_sessions(agents, {})
check("nested claude counts once", count, 2)
check("memory includes MCP children", held, 1900 * 2**20)

print("\nattention() — agent alert fires at the threshold and not below")
mem = collect.Memory(total=16 * 2**30, available=True)
disk = collect.Disk(total=0)
cfg_low = {"thresholds": {"agent_sessions_warn": 2}}
cfg_high = {"thresholds": {"agent_sessions_warn": 3}}
hit = [a.text for a in collect.attention(mem, disk, [], agents, cfg_low) if "agent sessions" in a.text]
miss = [a.text for a in collect.attention(mem, disk, [], agents, cfg_high) if "agent sessions" in a.text]
check("fires at 2 sessions with warn=2", len(hit), 1)
check("silent at 2 sessions with warn=3", len(miss), 0)
stack_alert = [a.text for a in collect.attention(mem, disk, [], [], {"thresholds": {}}, stacks=found)
               if "detached dev stack" in a.text]
check("a detached stack reaches NEEDS ATTENTION", len(stack_alert), 1)

print("\nleftover_browsers() — only adopted, stale automation sessions")
procs_b = [proc(700, 1, "node", age=3 * DAY), proc(710, 88, "node", age=3 * DAY),
           proc(720, 1, "node", age=3600)]
def sess(root, fam="automation-browser"):
    return collect.Session(family=fam, root_pid=root, mark="", detail="", binary="node",
                           age_seconds=next(p.age_seconds for p in procs_b if p.pid == root),
                           memory=500 * 2**20)
found_b = [sess(700), sess(710), sess(720), sess(700, fam="mcp-server")]
left = collect.leftover_browsers(procs_b, CFG, found_b)
check("adopted + stale kept, live parent / young / other family dropped",
      [(s.root_pid, s.family) for s in left], [(700, "automation-browser")])
hij = [a.text for a in collect.attention(mem, disk, [], [], {"thresholds": {}}, browsers=left)
       if "Dock" in a.text]
check("reaches NEEDS ATTENTION naming the Dock symptom", len(hij), 1)

print("\ncwd_for_pids() — one unreadable pid must not blank the rest")
mine = os.getpid()
got = collect.cwd_for_pids([1, mine])
check("own pid resolved despite root-owned pid 1", got.get(mine), os.getcwd())

print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED: {failures}'}")
sys.exit(1 if failures else 0)
