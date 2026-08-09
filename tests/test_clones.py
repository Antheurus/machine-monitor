#!/usr/bin/env python3
"""Clone detection and removal, proved in both directions.

A guard that only ever refuses is indistinguishable from a broken one, so every
refusal case here is paired with a case that must be allowed. Run directly:

    python3 tests/test_clones.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import actions  # noqa: E402
import clones  # noqa: E402

PASSED = FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok    {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def build_bucket(root: Path, app: str, count: int, payload: int = 2048) -> Path:
    """A tree shaped exactly like the real one: <root>/xx/yy/X/<app>.code_sign_clone."""
    bucket = root / "xx" / "yy" / "X" / f"{app}{clones.BUCKET_SUFFIX}"
    for index in range(count):
        member = bucket / f"{clones.MEMBER_PREFIX}{index:04d}" / "Contents" / "MacOS"
        member.mkdir(parents=True)
        (member / "binary").write_bytes(b"\0" * payload)
    return bucket


def test_detection(root: Path) -> clones.CloneGroup:
    build_bucket(root, "com.example.Litter", 5)
    build_bucket(root, "com.example.Single", 1)
    clones.CLONE_ROOT = root

    groups = clones.find_clone_groups(min_count=2)
    check("finds the bucket with 5 clones", len(groups) == 1, f"got {len(groups)} group(s)")
    group = groups[0]
    check("labels it by bundle id", group.label == "com.example.Litter", group.label)
    check("counts every member", group.count == 5, str(group.count))
    check("apparent size is non-zero", group.apparent_bytes > 0, str(group.apparent_bytes))

    # The other direction: a single clone is an app running normally, not litter.
    check("min_count excludes the 1-clone bucket",
          all(g.label != "com.example.Single" for g in groups))
    check("min_count=0 includes it",
          any(g.label == "com.example.Single" for g in clones.find_clone_groups(min_count=0)))
    return group


def test_in_use_is_not_guessed(group: clones.CloneGroup) -> None:
    unknown = clones.CloneGroup(
        base=group.base, label=group.label, members=list(group.members),
        apparent_bytes=group.apparent_bytes, oldest=group.oldest,
        newest=group.newest, in_use=None,
    )
    check("unknown in-use yields no removable targets", unknown.removable == [])
    report = clones.purge(unknown, actions.remove_dirs, confirm=True)
    check("unknown in-use removes nothing", report.results == [] and report.removed == 0)
    check("the clones are still on disk", len(os.listdir(group.base)) == 5)

    held = clones.CloneGroup(
        base=group.base, label=group.label, members=list(group.members),
        apparent_bytes=group.apparent_bytes, oldest=group.oldest,
        newest=group.newest, in_use=frozenset({group.members[0]}),
    )
    check("an open clone is excluded, the rest are not",
          held.removable == sorted(group.members[1:]))


def test_removal_guards(root: Path, group: clones.CloneGroup) -> None:
    base = group.base
    inside = os.path.join(base, group.members[0])

    check("allows a genuine member", actions.refuse_removal(inside, base, clones.MEMBER_PREFIX) == "")

    outside = root / "elsewhere"
    outside.mkdir()
    check("refuses a path outside the base",
          "resolves outside" in actions.refuse_removal(str(outside), base))

    link = os.path.join(base, f"{clones.MEMBER_PREFIX}link")
    os.symlink(str(outside), link)
    check("refuses a symlink", actions.refuse_removal(link, base) == "is a symlink")
    os.unlink(link)

    stray = os.path.join(base, "not-a-clone")
    os.mkdir(stray)
    check("refuses a name missing the marker",
          "does not contain" in actions.refuse_removal(stray, base, clones.MEMBER_PREFIX))
    os.rmdir(stray)

    check("refuses a protected base",
          "too broad" in actions.refuse_removal("/private/var/folders/x", "/private/var/folders"))
    check("reports a vanished target as absent",
          actions.refuse_removal(os.path.join(base, "gone"), base) == "no longer present")

    # Confirmation is not a formality: without it nothing may be deleted.
    unconfirmed = actions.remove_dirs([inside], base=base, confirm=False)
    check("refuses without confirm=True", unconfirmed[0].outcome == "refused")
    check("the target survived an unconfirmed call", os.path.isdir(inside))


def test_purge_measures_reality(group: clones.CloneGroup) -> None:
    dry = clones.purge(group, actions.remove_dirs, confirm=True, dry_run=True)
    check("dry run plans every clone",
          all(r.outcome == "planned" for r in dry.results) and len(dry.results) == 5)
    check("dry run deletes nothing", len(os.listdir(group.base)) == 5)

    real = clones.purge(group, actions.remove_dirs, confirm=True)
    check("removes all 5", real.removed == 5, str(real.removed))
    check("the bucket is empty", os.listdir(group.base) == [])
    check("apparent bytes were counted", real.apparent_bytes > 0)
    check("both free-space readings were taken",
          real.free_before > 0 and real.free_after > 0)
    check("real bytes are the measured delta",
          real.real_bytes == max(0, real.free_after - real.free_before))

    # Asserting that the two figures merely *differ* is not a test: deleting a few
    # kilobytes of ordinary files can move free space by exactly the apparent
    # amount, and the check then fails for a reason that has nothing to do with
    # the code. Force them apart instead and prove the real figure ignores the
    # apparent one outright.
    forced = clones.PurgeReport(group, [], apparent_bytes=10**12,
                                free_before=100, free_after=150)
    check("apparent size never leaks into the real figure", forced.real_bytes == 50)
    shrunk = clones.PurgeReport(group, [], apparent_bytes=0,
                                free_before=200, free_after=100)
    check("a negative delta clamps to zero", shrunk.real_bytes == 0)


def main() -> int:
    original_root = clones.CLONE_ROOT
    tmp = Path(tempfile.mkdtemp(prefix="mm-clone-test-"))
    try:
        print("detection")
        group = test_detection(tmp)
        print("in-use handling")
        test_in_use_is_not_guessed(group)
        print("removal guards")
        test_removal_guards(tmp, group)
        print("purge")
        test_purge_measures_reality(group)
    finally:
        clones.CLONE_ROOT = original_root
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
