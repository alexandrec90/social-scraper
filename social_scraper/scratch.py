"""An import's scratch directory under `data/`, and the sweep that clears a killed run's.

An import spools every input into DuckDB on disk (`backfill.Spool`), dry run included:
the counts a dry run prints are after de-duplication across files, which is the spool's
job, and capped at `SPOOL_MEMORY` it spills tens of GiB to its `tmp/`. A `with` block
removes that directory on any exit Python sees -- an exception, Ctrl-C -- but not on a
kill: on 2026-10-08 a StockTwits dry run reaped for low memory left 29.7 GiB in `data/`,
found and removed by hand.

So the directory's name records its owner, `import-<pid>-<random>`, and opening one
first sweeps `data/` for scratch directories whose owner is no longer running. A live
owner's is left alone: a long Reddit import shared the machine with that dry run, and
two imports at once is ordinary. A recycled pid can only make a dead run's directory
look alive, so the failure mode is keeping it a while longer, never deleting a live
spool; every directory kept is logged, so it is not kept unnoticed. A directory named
by the old `TemporaryDirectory(prefix="import-")`, which records no owner, is named in
the log and never deleted.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "import-"
# `tempfile`'s random suffix is drawn from [a-z0-9_], so it never holds a `-`: a name of
# the old spelling never matches.
OWNED = re.compile(rf"^{re.escape(PREFIX)}(\d+)-[a-z0-9_]+$")
GIB = 1024**3


def owner_pid(name: str) -> int | None:
    """The pid in a scratch directory's name, or None for any other name."""
    match = OWNED.match(name)
    return int(match.group(1)) if match else None


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid is running. Never signals it."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)  # signal 0 is an existence check on POSIX; on Windows it kills
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process
    return True


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    query_limited_information = 0x1000
    still_active = 259
    access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        # Access denied means it exists (a protected process); anything else, it does not.
        return ctypes.get_last_error() == access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # cannot tell: keeping a directory is the safe answer
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass  # removed while walking; the total is for a log line
    return total


def sweep(parent: Path, alive: Callable[[int], bool] = pid_alive) -> list[Path]:
    """Remove every scratch directory in `parent` whose owner is not running.

    Returns what was removed. A directory that cannot be removed is logged and left for
    the next sweep: clearing an old run's scratch must not stop this one.
    """
    if not parent.is_dir():
        return []
    removed = []
    for path in sorted(parent.iterdir()):
        if not path.name.startswith(PREFIX) or not path.is_dir():
            continue
        pid = owner_pid(path.name)
        if pid is None:
            log.warning(
                "%s: an import scratch directory that records no owner (%.1f GiB); "
                "remove it by hand once no import is running",
                path,
                _size(path) / GIB,
            )
            continue
        if alive(pid):
            log.info("%s: process %d is still running; left alone", path, pid)
            continue
        size = _size(path)
        try:
            shutil.rmtree(path)
        except OSError as exc:
            log.warning("%s: could not remove it (%s); the next import retries", path, exc)
            continue
        log.warning(
            "%s: removed %.1f GiB left by process %d, which is no longer running",
            path,
            size / GIB,
            pid,
        )
        removed.append(path)
    return removed


@contextmanager
def scratch_dir(parent: Path) -> Iterator[Path]:
    """A fresh `import-<pid>-*` directory in `parent`, after sweeping dead runs' ones."""
    parent.mkdir(parents=True, exist_ok=True)
    sweep(parent)
    path = Path(tempfile.mkdtemp(prefix=f"{PREFIX}{os.getpid()}-", dir=parent))
    try:
        yield path
    finally:
        shutil.rmtree(path)
