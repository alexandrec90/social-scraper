"""End a scheduled `scrape` that is still going when the next fire is due.

The cycle's deadlines (`runner.cycle_deadline`, `runner.export_deadline`) are checked
between steps, so a step that never returns outlives all of them. 2026-10-09 19:05 UTC:
Docker's VM froze for 27 minutes, a database call the 19:00 cycle had made blocked until
it thawed at 19:32, and devkit's scheduler skipped the 19:30 fire behind it -- filed on
devkit's ledger as overlapping runs (1590bcaa), after four fixes to the crawl's own pace.
Nothing in the process can interrupt a call blocked in a socket read, so at
`runner.hard_deadline` a `Watchdog` records the cycle as stuck, prints where it was
stuck, and ends the process.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TextIO


class Watchdog:
    """Calls `expire` at `at` and ends the process with the code it returns, unless the
    cycle claims its own ending first (`finish`). Exactly one of the two ends the run."""

    def __init__(
        self,
        at: datetime,
        expire: Callable[[], int],
        end: Callable[[int], object] = os._exit,
        now: datetime | None = None,
    ) -> None:
        delay = max(0.0, (at - (now or datetime.now(UTC))).total_seconds())
        self._claim = threading.Lock()
        self._expire, self._end = expire, end
        self._code: int | None = None
        self._timer = threading.Timer(delay, self._fire)
        self._timer.daemon = True

    def start(self) -> Watchdog:
        self._timer.start()
        return self

    def finish(self) -> bool:
        """Claim the ending for the cycle: True, and the watchdog will not fire; False
        when it already has, and the run's ending is `outcome`'s."""
        self._timer.cancel()
        return self._claim.acquire(blocking=False)

    def outcome(self) -> int:
        """The code the watchdog ended the run with, once it has. Only reached where
        `end` does not end the process -- a test's."""
        self._timer.join()
        return 1 if self._code is None else self._code

    def _fire(self) -> None:
        if not self._claim.acquire(blocking=False):
            return
        self._code = self._expire()
        sys.stdout.flush()
        sys.stderr.flush()
        self._end(self._code)


def print_stacks(stream: TextIO | None = None) -> None:
    """Where every other thread is, as tracebacks: the call a stuck cycle is blocked in."""
    out = stream or sys.stdout
    me = threading.get_ident()
    for ident, frame in sys._current_frames().items():
        if ident != me:
            print(f"thread {ident} was in:", file=out)
            traceback.print_stack(frame, file=out)
