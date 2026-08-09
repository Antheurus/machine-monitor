"""The only code here that destroys anything.

Kept in its own module so the destructive surface is one small file that can be
read in full before being trusted. Every path through it obeys the same three
rules: a target must come from a candidate list the caller built, it is checked
against a refusal list immediately before the act, and nothing is signalled or
deleted without the caller passing confirm=True.

Two kinds of target live here — processes, which are signalled, and directories,
which are removed. They share the contract and nothing else.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass

# Killing any of these takes the session or the machine down with it. The check
# is by name because pids are not stable, and it runs at signal time rather than
# at list-build time so a recycled pid cannot slip through.
PROTECTED_NAMES = frozenset({
    "kernel_task", "launchd", "WindowServer", "loginwindow", "systemuiserver",
    "Finder", "Dock", "coreaudiod", "opendirectoryd", "securityd", "sshd",
    "logind", "distnoted", "mds", "mds_stores", "mdworker", "syslogd",
})

PROTECTED_PIDS = frozenset({0, 1})


@dataclass
class KillResult:
    pid: int
    name: str
    outcome: str  # "terminated" | "killed" | "gone" | "planned" | "refused" | "error"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in ("terminated", "killed", "gone", "planned")


def _process_name(pid: int) -> str:
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip().rsplit("/", 1)[-1]


def _ps_field(pid: int, field: str) -> str:
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", f"{field}="],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(out.stdout.split())


def refuse_reason(pid: int, expected_name: str = "", expected_argv_mark: str = "",
                  expected_start: str = "") -> str:
    """Why this pid must not be signalled, or '' when it may be.

    Re-reads the process rather than trusting the caller's copy: between building
    a list and acting on it the pid may have died and been reused, and catching
    that is the whole point.

    `expected_name` alone cannot do it for a multi-process app, because every
    process shares one executable — the user's own Chrome and a Playwright Chrome
    both report `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`,
    so a recycled pid passes the name check and the human's browser is signalled.

    Two stronger checks, and they answer different questions.
    `expected_start` is the process's `lstart`: it identifies this exact process
    INSTANCE, so it settles pid reuse outright and applies to every process,
    including a browser helper whose argv is indistinguishable from the user's.
    `expected_argv_mark` is a literal only the intended target carries (a
    throwaway `--user-data-dir`, a daemon's session name) and catches a caller
    that has confused two live processes rather than one recycled pid.
    """
    if pid in PROTECTED_PIDS:
        return "system pid"
    if pid == os.getpid() or pid == os.getppid():
        return "this monitor, or the shell running it"
    live_name = _process_name(pid)
    if not live_name:
        return "no longer running"
    if live_name in PROTECTED_NAMES:
        return f"protected process ({live_name})"
    if expected_name and live_name != expected_name:
        return f"pid now belongs to {live_name}, not {expected_name}"
    if expected_start:
        live_start = _ps_field(pid, "lstart")
        if live_start and live_start != expected_start:
            return f"started {live_start}, not {expected_start} — pid was reused"
    if expected_argv_mark and expected_argv_mark not in _ps_field(pid, "args"):
        return f"argv no longer contains {expected_argv_mark!r} — pid was reused"
    return ""


def terminate(pid: int, expected_name: str = "", grace: float = 3.0,
              confirm: bool = False, dry_run: bool = False,
              expected_argv_mark: str = "", expected_start: str = "") -> KillResult:
    """SIGTERM, then SIGKILL only if the process is still alive after `grace`.

    A dev server given SIGTERM closes its listening socket and flushes; SIGKILL
    as a first resort leaves the port in TIME_WAIT and can corrupt whatever it
    was writing.
    """
    name = expected_name or _process_name(pid) or str(pid)
    if not confirm:
        return KillResult(pid, name, "refused", "not confirmed")

    reason = refuse_reason(pid, expected_name, expected_argv_mark, expected_start)
    if reason:
        outcome = "gone" if reason == "no longer running" else "refused"
        return KillResult(pid, name, outcome, reason)

    if dry_run:
        return KillResult(pid, name, "planned", "dry run — would be terminated")

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return KillResult(pid, name, "gone", "exited before the signal")
    except PermissionError:
        return KillResult(pid, name, "error", "not permitted — owned by another user")
    except OSError as exc:
        return KillResult(pid, name, "error", str(exc))

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        time.sleep(0.15)
        if not _alive(pid):
            return KillResult(pid, name, "terminated")

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return KillResult(pid, name, "terminated")
    except OSError as exc:
        return KillResult(pid, name, "error", f"SIGTERM ignored, SIGKILL failed: {exc}")

    time.sleep(0.2)
    if _alive(pid):
        return KillResult(pid, name, "error", "survived SIGKILL")
    return KillResult(pid, name, "killed", f"ignored SIGTERM for {grace:g}s")


def _alive(pid: int) -> bool:
    """Whether the pid is a running process.

    `os.kill(pid, 0)` alone is not enough: a process that has exited but not yet
    been reaped by its parent stays in the process table as a zombie and answers
    signal 0 successfully. Trusting it made a successful kill report "survived
    SIGKILL" — the most alarming possible way to describe a process that is in
    fact dead. The signal probe stays as the fast path; the state read only runs
    when it claims the process is still there.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state="],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    state = out.stdout.strip()
    return bool(state) and not state.startswith("Z")


def terminate_all(targets: list[tuple[int, str]] | list[tuple[int, str, str]],
                  confirm: bool = False, dry_run: bool = False) -> list[KillResult]:
    """Signal a batch, parents before children.

    Descending pid order approximates parent-first, and killing a browser's main
    process usually takes its helpers with it — so most children are already gone
    by the time their turn comes, and they report "gone" rather than erroring.

    A target may carry a third element (the argv mark) and a fourth (the
    `lstart` string), both re-checked by `refuse_reason`.
    """
    results = []
    for target in sorted(targets, key=lambda item: item[0]):
        pid, name = target[0], target[1]
        mark = target[2] if len(target) > 2 else ""
        start = target[3] if len(target) > 3 else ""
        results.append(terminate(pid, name, confirm=confirm, dry_run=dry_run,
                                 expected_argv_mark=mark, expected_start=start))
    return results


# ── directories ──────────────────────────────────────────────────────────────

# A base this shallow means the caller computed a path wrong, and the damage
# from acting on it is unbounded. Refusing costs a re-run; not refusing does not.
PROTECTED_BASES = frozenset({
    "/", "/System", "/Users", "/Applications", "/Library", "/private",
    "/private/var", "/private/var/folders", "/private/tmp", "/tmp", "/usr",
    "/bin", "/sbin", "/etc", "/opt", "/home", "/Volumes",
})


@dataclass
class RemoveResult:
    path: str
    outcome: str  # "removed" | "planned" | "gone" | "refused" | "error"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in ("removed", "planned", "gone")


def refuse_removal(path: str, base: str, name_must_contain: str = "") -> str:
    """Why this directory must not be removed, or '' when it may be.

    Re-resolves the path rather than trusting the caller's string, for the same
    reason `refuse_reason` re-reads a pid: between building a list and acting on
    it the filesystem may have changed underneath.

    The containment check is the load-bearing one, and it is written against
    `realpath` on both sides because a symlinked parent otherwise lets a target
    that reads as inside `base` resolve anywhere at all. Direct string prefixing
    on the unresolved paths would pass `/tmp/safe-evil` for a base of
    `/tmp/safe`, so the separator is part of the comparison.
    """
    if not os.path.isabs(path) or not os.path.isabs(base):
        return "not an absolute path"

    real_base = os.path.realpath(base).rstrip(os.sep) or os.sep
    if real_base in PROTECTED_BASES:
        return f"base {real_base} is too broad to sweep"
    if real_base == os.path.realpath(os.path.expanduser("~")):
        return "base is the home directory"

    if os.path.islink(path):
        return "is a symlink"
    if not os.path.lexists(path):
        return "no longer present"

    real = os.path.realpath(path)
    if not real.startswith(real_base + os.sep):
        return f"resolves outside {real_base}"
    if name_must_contain and name_must_contain not in os.path.basename(real):
        return f"name does not contain {name_must_contain!r}"
    if not os.path.isdir(real):
        return "not a directory"
    return ""


def remove_dirs(paths: list[str], base: str, name_must_contain: str = "",
                confirm: bool = False, dry_run: bool = False) -> list[RemoveResult]:
    """Delete directories that all sit directly under one verified `base`.

    Every target is enumerated by the caller and re-checked here. A `*` or `.`
    pathspec never reaches this function, because there is nowhere to put one —
    the signature takes a list, and a wildcard that expanded wider than intended
    is the classic way a sweep destroys the files it was written to protect.
    """
    results: list[RemoveResult] = []
    for path in paths:
        if not confirm:
            results.append(RemoveResult(path, "refused", "not confirmed"))
            continue

        reason = refuse_removal(path, base, name_must_contain)
        if reason == "no longer present":
            results.append(RemoveResult(path, "gone", reason))
            continue
        if reason:
            results.append(RemoveResult(path, "refused", reason))
            continue
        if dry_run:
            results.append(RemoveResult(path, "planned", "dry run — would be removed"))
            continue

        try:
            shutil.rmtree(path)
        except OSError as exc:
            results.append(RemoveResult(path, "error", str(exc)))
            continue
        results.append(RemoveResult(path, "removed"))
    return results
