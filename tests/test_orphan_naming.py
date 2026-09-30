"""An orphan's name must survive an executable path that contains spaces.

Both directions on one input: the genuine target must be permitted and a
recycled pid must still be refused. A check that only ever refuses is
indistinguishable from a broken one.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import actions  # noqa: E402
import collect  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Versions/151.0.7922.109/Helpers/Google Chrome Helper (GPU).app/Contents/MacOS/Google Chrome Helper (GPU)"
PROFILE = "/var/folders/jz/xxxx/T/playwright_chromiumdev_profile-2QObZy"
ARGS = f"{CHROME} --type=gpu-process --user-data-dir={PROFILE}"
PID = 999001

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}\n        got {got!r}\n        want {want!r}")
    if not ok:
        failures.append(label)


def fake_ps(cmd, timeout=0):
    return f"{PID} {ARGS}\n"


class FakeProc:
    pid = PID
    name = "Google Chrome Helper (GPU)"
    age_seconds = 3600.0
    memory = 220 * 2**20


print("1. orphan_automation derives the name from ps comm=, not from argv")
real_run = collect.run
collect.run = fake_ps
try:
    orphans = collect.orphan_automation([FakeProc()])
finally:
    collect.run = real_run

check("one orphan matched", len(orphans), 1)
check("binary is the full comm basename", orphans[0].binary, "Google Chrome Helper (GPU)")
check("profile captured for use as the argv mark", orphans[0].profile, PROFILE)

print("\n2. refuse_reason PERMITS the genuine target with that name")


def stub_identity(name, args):
    actions._process_name = lambda pid: name
    actions._ps_field = lambda pid, field: args if field == "args" else ""


orig_name, orig_field = actions._process_name, actions._ps_field
try:
    stub_identity("Google Chrome Helper (GPU)", ARGS)
    check("permitted (empty reason)",
          actions.refuse_reason(PID, orphans[0].binary, orphans[0].profile), "")

    print("\n3. the pre-fix name is REFUSED — this is what broke --kill-orphans")
    prefix_name = ARGS.split(" ", 1)[0].rsplit("/", 1)[-1]
    check("pre-fix derivation yields the truncated name", prefix_name, "Google")
    check("and the guard rejects its own target",
          actions.refuse_reason(PID, prefix_name, orphans[0].profile),
          "pid now belongs to Google Chrome Helper (GPU), not Google")

    print("\n4. a recycled pid is still REFUSED — the guard did not go slack")
    stub_identity("Google Chrome Helper (GPU)", CHROME + " --user-data-dir=/Users/x/Library/Application Support/Google/Chrome")
    reason = actions.refuse_reason(PID, orphans[0].binary, orphans[0].profile)
    check("refused on the argv mark", reason.startswith("argv no longer contains"), True)

    stub_identity("Slack", ARGS)
    check("refused on a changed name",
          actions.refuse_reason(PID, orphans[0].binary, orphans[0].profile),
          "pid now belongs to Slack, not Google Chrome Helper (GPU)")
finally:
    actions._process_name, actions._ps_field = orig_name, orig_field

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
