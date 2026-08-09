"""Directories whose apparent size lies.

macOS clones an app bundle to validate its code signature and files the copy
under `/private/var/folders/<x>/<y>/X/<bundle-id>.code_sign_clone/`. The clones
are meant to be reaped and frequently are not: 258 copies of `Google Chrome.app`
accumulated in that one bucket over eight days, 652 GB apparent, growing by
about 32 a day.

The number is the whole trap. `du` bills a clone at full size, and so does the
Storage pane in System Settings — which is why a Mac with 79 GB free reports
"System Data 180.52 GB" and why `du -sh -x /private/var/folders` answered 609 G
on a volume holding 375 GB. Every block is shared with the original app, so the
litter is not recoverable disk. Deleting all 258 returned 2.4 GB.

Two consequences shape this module. Apparent size is reported as an upper bound
and never as a promise, and the real figure comes from `statvfs` read before and
after the removal rather than from summing what was deleted. The second habit is
the one worth carrying elsewhere: the two numbers differed by 260x here, and
only the measured one is what the user gets back.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Only the per-user temp hierarchy holds these, and only its `X` bucket.
CLONE_ROOT = Path("/private/var/folders")
BUCKET_SUFFIX = ".code_sign_clone"
MEMBER_PREFIX = "code_sign_clone."

# `lsof +D` walks the whole tree, which is a quarter of a million files at the
# sizes this module exists for. Slow is acceptable; guessing is not.
LSOF_TIMEOUT = 90.0


@dataclass
class CloneGroup:
    """One app's clone bucket."""

    base: str
    label: str
    members: list[str]
    apparent_bytes: int
    oldest: float
    newest: float
    # None means lsof could not answer. Distinct from an empty set, which means
    # it answered and nothing is open — conflating them would delete a live clone.
    in_use: frozenset[str] | None

    @property
    def count(self) -> int:
        return len(self.members)

    @property
    def removable(self) -> list[str]:
        if self.in_use is None:
            return []
        return sorted(name for name in self.members if name not in self.in_use)


@dataclass
class PurgeReport:
    group: CloneGroup
    results: list
    apparent_bytes: int
    free_before: int
    free_after: int

    @property
    def removed(self) -> int:
        return sum(1 for r in self.results if r.outcome == "removed")

    @property
    def real_bytes(self) -> int:
        """What the volume actually got back.

        Clamped at zero because another process writing during the sweep can
        make the delta negative, and a negative reclaim figure reads as a bug in
        the tool rather than as unrelated disk activity.
        """
        return max(0, self.free_after - self.free_before)


def apparent_size(path: str | Path) -> int:
    """Allocated bytes below `path`, counted the way `du` counts them.

    Clones are billed in full and sparse files are billed at their real blocks,
    which is exactly the mix that makes this figure untrustworthy as a reclaim
    estimate. It is still the right number for *finding* the litter.
    """
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_blocks * 512
            except OSError:
                continue
    return total


def free_bytes(path: str | Path = "/System/Volumes/Data") -> int:
    """Space available to an unprivileged writer, from the filesystem itself."""
    try:
        stat = os.statvfs(path)
    except OSError:
        return 0
    return stat.f_bavail * stat.f_frsize


def open_members(base: str | Path) -> frozenset[str] | None:
    """Which clone directories a running process currently holds open.

    Returns None when that cannot be established, so the caller can refuse
    rather than assume. `lsof` exits non-zero whenever any single descriptor is
    unreadable, which is routine on a busy machine — so stdout is read
    regardless of the return code, and only a failure to run at all is fatal.
    """
    base = str(base)
    try:
        proc = subprocess.run(
            ["lsof", "-n", "-Fn", "+D", base],
            capture_output=True, text=True, timeout=LSOF_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    prefix = base.rstrip(os.sep) + os.sep
    found = set()
    for line in proc.stdout.splitlines():
        if not line.startswith("n"):
            continue
        path = line[1:]
        if not path.startswith(prefix):
            continue
        found.add(path[len(prefix):].split(os.sep, 1)[0])
    return frozenset(found)


def find_clone_groups(min_count: int = 2, probe_open: bool = True) -> list[CloneGroup]:
    """Every clone bucket holding at least `min_count` copies, biggest first."""
    groups: list[CloneGroup] = []
    for bucket_root in sorted(CLONE_ROOT.glob("*/*/X")):
        if not bucket_root.is_dir() or bucket_root.is_symlink():
            continue
        try:
            buckets = sorted(bucket_root.iterdir())
        except OSError:
            continue
        for bucket in buckets:
            group = _read_bucket(bucket, min_count, probe_open)
            if group is not None:
                groups.append(group)
    groups.sort(key=lambda g: g.apparent_bytes, reverse=True)
    return groups


def _read_bucket(bucket: Path, min_count: int, probe_open: bool) -> CloneGroup | None:
    if bucket.is_symlink() or not bucket.is_dir():
        return None
    try:
        entries = list(bucket.iterdir())
    except OSError:
        return None

    members, times = [], []
    for entry in entries:
        if entry.is_symlink() or not entry.name.startswith(MEMBER_PREFIX):
            continue
        try:
            stat = entry.lstat()
        except OSError:
            continue
        if not entry.is_dir():
            continue
        members.append(entry.name)
        times.append(stat.st_mtime)

    if len(members) < min_count or not members:
        return None

    label = bucket.name
    if label.endswith(BUCKET_SUFFIX):
        label = label[: -len(BUCKET_SUFFIX)]

    return CloneGroup(
        base=str(bucket),
        label=label,
        members=sorted(members),
        apparent_bytes=apparent_size(bucket),
        oldest=min(times),
        newest=max(times),
        in_use=open_members(bucket) if probe_open else None,
    )


def purge(group: CloneGroup, remover, confirm: bool = False,
          dry_run: bool = False) -> PurgeReport:
    """Remove every clone in `group` that no process holds open.

    `remover` is `actions.remove_dirs`, injected so the only code in this skill
    that deletes anything stays in the one module written to be read in full.
    The free-space reading is taken immediately either side of the removal —
    predicting it from the deleted bytes is the mistake this module exists to
    stop.
    """
    targets = group.removable
    before = free_bytes(group.base)

    if group.in_use is None:
        return PurgeReport(group, [], 0, before, before)

    apparent = sum(apparent_size(os.path.join(group.base, name)) for name in targets)
    results = remover(
        [os.path.join(group.base, name) for name in targets],
        base=group.base,
        name_must_contain=MEMBER_PREFIX,
        confirm=confirm,
        dry_run=dry_run,
    )
    return PurgeReport(group, results, apparent, before, free_bytes(group.base))
