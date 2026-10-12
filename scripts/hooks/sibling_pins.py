#!/usr/bin/env python3
"""Who is building against a shared path-source sibling, at which commit, so nobody moves it
under them.

Every worktree of a tier resolves a `[tool.uv.sources]` path such as ibkr_trader's
`../data-lake` to one directory -- `.claude/worktrees/data-lake` -- and uv takes no
per-run override of a path source, so one sibling per pin cannot exist: the sibling is
shared, and its commit is what has to be coordinated. `toolchain.align_sibling` puts it
at a tree's CI pin before each run and devkit's `worktree_env` before each provision;
before this, either moved it under a concurrent run in another tree on another pin, whose
`uv run` then relocked or type-checked against the wrong commit (d32ffcc2: the #116
fixer's `worktree.py provision` moved data-lake d32d4d5 -> c0aa963 while the #123 fixer
was checking its lock at d32d4d5).

- A run building against the sibling holds a **lease**: a file in the sibling's own git
  directory naming the commit and the run's pid. Leases at one commit coexist.
- A **move** happens only while no live lease names another commit, under a lock held
  from that check until the mover's own lease is written, so two movers cannot both pass.
- A lease is live while its pid is, and never past `MAX_AGE`: a run that died leaves a
  file nothing waits on. The lease is dropped when the process that took it exits.

A command run by hand (`uv lock --check`) takes no lease and is not protected; the
runners and the provisioner are, which is every move this harness makes.

Stdlib only: vendored, and imported by devkit's `worktree_env`, which git runs as a hook.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

# Beside git's own per-worktree files, so the sibling's working tree never shows them.
LEASES = "devkit-pin-leases"
LEASE_SUFFIX = ".lease"
LOCK = "move.lock"
# A lease older than this is not waited on whatever its pid says: Windows reuses pids.
MAX_AGE = 6 * 3600
# A move lock older than this was left by a mover that died mid-move; a move is seconds.
LOCK_STALE = 120
# How long a move waits for runs at another commit, then gives up and leaves it.
WAIT = 900
POLL = 2.0

Alive = Callable[[int], bool]


@dataclass(frozen=True)
class Lease:
    """A run building against the sibling at `sha`, from `tree`."""

    pid: int
    sha: str
    tree: str
    path: Path

    @property
    def line(self) -> str:
        return f"a run in {self.tree} (pid {self.pid}) building against {self.sha[:7]}"


def lease_dir(target: Path) -> Path | None:
    """`<target's own git dir>/devkit-pin-leases`; None when `target` is no linked worktree."""
    try:
        text = (target / ".git").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:") :].strip())
    return (gitdir if gitdir.is_absolute() else target / gitdir) / LEASES


def pid_alive(pid: int) -> bool:
    """Whether `pid` names a running process. Unreadable reads as alive: waiting is safe.

    On Windows an open handle outlives its process, so it is waited on with no timeout
    rather than trusted; `os.kill(pid, 0)` there would terminate the process.
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":  # pragma: no cover - the suite runs on Windows too
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    synchronize, wait_timeout, access_denied = 0x00100000, 0x102, 5
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return ctypes.get_last_error() == access_denied
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def live_leases(directory: Path, now: float, alive: Alive = pid_alive) -> list[Lease]:
    """Every lease in `directory` whose run is still going."""
    try:
        files = sorted(directory.glob(f"*{LEASE_SUFFIX}"))
    except OSError:
        return []
    found = []
    for path in files:
        try:
            pid = int(path.name[: -len(LEASE_SUFFIX)])
            sha, _, tree = path.read_text(encoding="utf-8").partition("\n")
            fresh = now - path.stat().st_mtime < MAX_AGE
        except (OSError, ValueError):
            continue
        if sha.strip() and fresh and alive(pid):
            found.append(Lease(pid, sha.strip(), tree.strip(), path))
    return found


def blockers(directory: Path, sha: str, pid: int, now: float, alive: Alive = pid_alive):
    """The live leases of other runs at a commit other than `sha`: what a move would break."""
    return [
        lease
        for lease in live_leases(directory, now, alive)
        if lease.pid != pid and lease.sha != sha
    ]


def hold(directory: Path, sha: str, tree: Path, pid: int) -> Path:
    """Write `pid`'s lease on `sha`; dropped when this process exits."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}{LEASE_SUFFIX}"
    path.write_text(f"{sha}\n{tree}\n", encoding="utf-8")
    if pid == os.getpid():
        atexit.register(release, path)
    return path


def release(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _take_lock(path: Path, now: float, pid: int, alive: Alive) -> bool:
    try:
        holder = int(path.read_text(encoding="utf-8").strip() or 0)
        stale = now - path.stat().st_mtime > LOCK_STALE or not alive(holder)
    except (OSError, ValueError):
        stale = path.exists()
    if stale:
        release(path)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    with os.fdopen(handle, "w", encoding="utf-8") as lock:
        lock.write(f"{pid}\n")
    return True


@contextlib.contextmanager
def move_lock(directory: Path, pid: int, alive: Alive = pid_alive) -> Iterator[bool]:
    """Hold the sibling's move lock while it is free; yields whether it was had."""
    path = directory / LOCK
    try:
        directory.mkdir(parents=True, exist_ok=True)
        held = _take_lock(path, time.time(), pid, alive)
    except OSError:
        held = False
    try:
        yield held
    finally:
        if held:
            release(path)


@dataclass(frozen=True)
class Claim:
    """How a claim on the sibling at one commit ended.

    `held` is the lease taken (None when none was); `blocked` the runs at another
    commit it gave up waiting on; `moved` whether `move` ran and succeeded.
    """

    held: Path | None
    blocked: tuple[Lease, ...] = ()
    moved: bool = False


def _say(line: str) -> None:
    print(line, file=sys.stderr)


@dataclass(frozen=True)
class Pace:
    """How a claim waits, and the process and clocks it waits with -- seams for a test."""

    wait: float = WAIT
    pid: int = 0  # 0: this process
    alive: Alive = pid_alive
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    say: Callable[[str], None] = _say


@dataclass(frozen=True)
class Sibling:
    """The shared tree, the commit a run wants it at, and how to read and move its HEAD."""

    target: Path
    want: str
    head: Callable[[], str]
    move: Callable[[], bool]


def _attempt(sibling: Sibling, directory: Path, tree: Path, me: int, alive: Alive):
    """One try under the move lock: the `Claim` it settled, else the leases in its way."""
    with move_lock(directory, me, alive) as locked:
        if not locked:
            return []
        if sibling.head() == sibling.want:
            return Claim(hold(directory, sibling.want, tree, me))
        waited = blockers(directory, sibling.want, me, time.time(), alive)
        if waited:
            return waited
        if not sibling.move():
            return Claim(None)
        return Claim(hold(directory, sibling.want, tree, me), moved=True)


def claim(sibling: Sibling, tree: Path, pace: Pace | None = None) -> Claim:
    """Have the shared tree at `sibling.want` for this run, and lease it there for `tree`.

    Already there: leased at once. Otherwise `move` runs once no other run's live lease
    names another commit, waiting up to `pace.wait` seconds; then the claim gives up and
    names who holds it. A failed `move` leases nothing. With no lease directory (not a
    linked worktree) `move` runs unguarded, as before this existed.
    """
    pace = pace or Pace()
    me = pace.pid or os.getpid()
    directory = lease_dir(sibling.target)
    if directory is None:
        return Claim(None, moved=sibling.head() != sibling.want and sibling.move())
    deadline = pace.clock() + pace.wait
    told = False
    while True:
        outcome = _attempt(sibling, directory, tree, me, pace.alive)
        if isinstance(outcome, Claim):
            return outcome
        if pace.clock() >= deadline:
            return Claim(None, blocked=tuple(outcome))
        if outcome and not told:
            pace.say(
                f"waiting up to {pace.wait:.0f}s to move {sibling.target} to "
                f"{sibling.want[:7]}: " + "; ".join(lease.line for lease in outcome)
            )
            told = True
        pace.sleep(POLL)
