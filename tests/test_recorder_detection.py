"""A recorder is matched on its whole executable name, not on a path substring.

Both directions: Obsidian must not read as OBS, and a real OBS must still be found.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import collect  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}\n        got {got!r}\n        want {want!r}")
    if not ok:
        failures.append(label)


OBSIDIAN = """  901 /Applications/Obsidian.app/Contents/MacOS/Obsidian
  902 /Applications/Obsidian.app/Contents/Frameworks/Obsidian Helper (Renderer).app/Contents/MacOS/Obsidian Helper (Renderer)
  903 /usr/libexec/nsurlsessiond
  904 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
"""

RECORDERS = """  911 /Applications/OBS.app/Contents/MacOS/OBS
  912 /Applications/zoom.us.app/Contents/MacOS/zoom.us
  913 /Applications/CleanShot X.app/Contents/MacOS/CleanShot X
  914 /System/Applications/QuickTime Player.app/Contents/MacOS/QuickTime Player
"""

print("1. Obsidian and its helpers are not OBS")
check("no recorder found", collect.running_recorders(OBSIDIAN), [])

print("\n2. real recorders are still found, including a name with a suffix")
check("all four found", collect.running_recorders(RECORDERS),
      ["QuickTime Player", "OBS", "CleanShot", "zoom.us"])

print("\n3. lowercase obs binary still matches")
check("found", collect.running_recorders("  920 /opt/homebrew/bin/obs\n"), ["OBS"])

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
