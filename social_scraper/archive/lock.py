"""One writer at a time per lake object: an exclusive lock file beside what it guards.

Writing a partition is read-merge-write, and so is recording it in the catalog. Two
processes doing either at once -- the scheduled export and a `reddit-import` -- each
read the old object and the second write erases the first's addition. On 2026-10-06
that dropped two of 4,018 backfilled days from `_catalog/social_scraper_posts.json`;
both writers also staged into the same `.tmp` file, so one could fail outright.

The lock is a file created with `O_CREAT | O_EXCL`, which the OS grants to exactly one
process, on Windows as on POSIX. It is held for one object's update -- milliseconds
locally, seconds against the bucket -- so a lock older than `STALE_S` was left by a
writer that died holding it, and is broken. Two waiters breaking the same stale lock at
once can still both proceed; that needs a crash first, and costs at worst the one entry
this module exists to protect.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_SUFFIX = ".lock"
# Far beyond any one update, so only a dead writer's lock is ever this old.
STALE_S = 300.0
# A waiter gives up after this; it outlasts STALE_S so a dead writer's lock is broken
# before anyone gives up on it.
TIMEOUT_S = 600.0
POLL_S = 0.05


class LockTimeout(OSError):
    """Another writer held the lock for longer than the timeout.

    An `OSError`, so an export fails the dataset and retries next run, as for a full disk.
    """


def _is_stale(path: Path, stale_s: float, now: Callable[[], float]) -> bool:
    try:
        return now() - path.stat().st_mtime > stale_s
    except FileNotFoundError:
        return False  # released between the failed create and this look


@contextmanager
def file_lock(
    path: Path,
    *,
    timeout_s: float = TIMEOUT_S,
    stale_s: float = STALE_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
) -> Iterator[None]:
    """Hold `path` exclusively for the `with` block. Raises `LockTimeout` on a long wait."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = monotonic() + timeout_s
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        # PermissionError: Windows answers a create on a file being deleted with it.
        except (FileExistsError, PermissionError):
            if _is_stale(path, stale_s, wall):
                path.unlink(missing_ok=True)
                continue
            if monotonic() >= deadline:
                message = f"{path}: held by another writer for over {timeout_s:.0f} s"
                raise LockTimeout(message) from None
            sleep(POLL_S)
    try:
        os.write(fd, f"{os.getpid()}\n".encode())  # who holds it, for a person looking
    finally:
        os.close(fd)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
