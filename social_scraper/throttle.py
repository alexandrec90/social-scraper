"""Pacing that keeps the accounts from getting banned.

Every page load goes through `Throttle.acquire()` first and reports back through
`Throttle.record()`. What it enforces, per platform:

- **Spacing** -- a uniform-random gap in `[min_delay, max_delay]` since the previous
  request. Jitter matters: a fixed interval is the easiest bot signature there is.
- **Budgets** -- at most N requests per rolling hour and per rolling day, counted from
  the persisted request log, so restarting the process does not reset them.
- **Active hours** -- outside the window the platform is simply skipped.
- **Deadline** -- an optional time after which no page load starts, so a scheduled
  cycle ends before the next one is due (`runner.cycle_deadline`).
- **Circuit breaker** -- a rate-limit response backs off exponentially
  (`backoff_base * 2^failures`, capped), counting the day's failed loads as well as
  the streak, so limits with good loads between them still slow the pace; a challenge (captcha, login wall, locked
  account) opens the circuit for `block_cooldown` and needs no further failures to do
  so -- and a challenge again on the first request after it doubles the cooldown
  (capped at `block_cooldown_max`), because on an always-on machine a fixed retry
  interval keeps knocking on a door the platform has already closed. A success closes
  it and resets the escalation. To an anonymous crawler the first challenge is a notice
  and the one after a cooldown an error (`Throttle.notice`): the pause is the response,
  and a block only needs a person once it has outlived one. On every platform, X
  included, a rate limit is a notice while the breaker can still answer it: the load
  before it succeeded, and the pause it set is short of `backoff_max`. A limit on the
  first load after a pause, or one the longest pause has to answer, is an error.

The store is a protocol so the rules are unit-tested against memory and run against
Postgres (`db.throttle_store.DbThrottleStore`).
"""

from __future__ import annotations

import logging
import random
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

OK = "ok"
RATE_LIMITED = "rate_limited"
CHALLENGED = "challenged"
ERROR = "error"
# The page load got no answer in time (`PageResult.timed_out`). A plain error to the
# breaker; to a crawler, a notice until the platform has failed through a pause
# (`Throttle.notice`), since one slow load on a loaded machine is not the platform down.
TIMED_OUT = "timed_out"
# The platform answered with a 5xx, or not at all (a reset connection): its own trouble,
# and mostly brief. A timeout's rules: on 2026-10-23 Bluesky's AppView answered one
# search with HTTP 500 between good ones, and the scheduled scrape failed on it.
SERVER_ERROR = "server_error"
# Failed loads in a row at which a plain error pauses the platform (`Throttle.failing`),
# which ends a crawler's run.
ERROR_STREAK = 3
# The outcomes that are a notice until a load after the `ERROR_STREAK` pause fails too
# (`Throttle.notice`). The streak itself is not an error: on 2026-10-28 the AppView
# answered three searches with HTTP 500 inside 25 seconds, the pause was the whole
# response, and failing the scrape on it only sent a fixer at an outage nobody could act on.
TRANSIENT = (TIMED_OUT, SERVER_ERROR)
# Failed loads in a row at which a challenge on an anonymous crawl stops being a notice
# (`Throttle.notice`). The first is one: the breaker has already paused the platform for
# `block_cooldown`, and only waiting lifts a block nobody is logged in to clear. A
# challenge again on the first load after that cooldown is a block that held.
CHALLENGE_STREAK = 2
# How far back a rate limit's backoff counts failed loads. The breaker's streak is not
# enough: a success resets it, so a limit hit once a cycle with good loads between would
# be paused for `backoff_base` every time and never slow the pace, nor reach the cap that
# makes it an error. On 2026-10-12 X served one rate-limit page after some 120 clean
# loads in 30 hours, at 6 an hour against a budget of 8; on 2026-10-23, two 90 minutes
# apart. Only waiting lifted either, so neither is an error until the waits run out.
RATE_LIMIT_WINDOW = timedelta(hours=24)
# The page wants a sign-in. Not the platform pushing back -- our own state -- so it is
# logged but leaves the circuit breaker alone; the scraper decides what to skip.
LOGIN_REQUIRED = "login_required"


class ThrottleStop(Exception):
    """The platform must not be touched now. Callers skip it for this cycle."""


class BudgetExhausted(ThrottleStop):
    pass


class CircuitOpen(ThrottleStop):
    pass


class OutsideActiveHours(ThrottleStop):
    pass


class OutOfTime(ThrottleStop):
    """The cycle's time is spent. A crawler records it as a notice and ends its run."""


@dataclass(frozen=True)
class Policy:
    platform: str
    min_delay_s: float
    max_delay_s: float
    hourly_budget: int
    daily_budget: int
    backoff_base_minutes: float = 15.0
    backoff_max_minutes: float = 360.0
    block_cooldown_hours: float = 12.0
    block_cooldown_max_hours: float = 96.0
    active_hours: tuple[int, int] | None = None
    timezone: str = "UTC"
    # No page load starts after this (aware) time; None is no limit.
    deadline: datetime | None = None


@dataclass
class Block:
    blocked_until: datetime | None
    reason: str | None
    consecutive_failures: int


class ThrottleStore(Protocol):
    def count_since(self, platform: str, since: datetime) -> int: ...
    def failures_since(self, platform: str, since: datetime) -> int: ...
    def last_request_at(self, platform: str) -> datetime | None: ...
    def log_request(
        self, platform: str, at: datetime, kind: str, status: int | None, ok: bool
    ) -> None: ...
    def get_block(self, platform: str) -> Block: ...
    def set_block(self, platform: str, block: Block, now: datetime) -> None: ...


class MemoryThrottleStore:
    """In-process store: tests, and one-off runs with no database."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, datetime, str, int | None, bool]] = []
        self.blocks: dict[str, Block] = {}

    def count_since(self, platform: str, since: datetime) -> int:
        return sum(1 for p, at, *_ in self.requests if p == platform and at >= since)

    def failures_since(self, platform: str, since: datetime) -> int:
        return sum(1 for p, at, *_, ok in self.requests if p == platform and at >= since and not ok)

    def last_request_at(self, platform: str) -> datetime | None:
        times = [at for p, at, *_ in self.requests if p == platform]
        return max(times) if times else None

    def log_request(
        self, platform: str, at: datetime, kind: str, status: int | None, ok: bool
    ) -> None:
        self.requests.append((platform, at, kind, status, ok))

    def get_block(self, platform: str) -> Block:
        return self.blocks.get(platform, Block(None, None, 0))

    def set_block(self, platform: str, block: Block, now: datetime) -> None:
        self.blocks[platform] = block


def in_active_hours(hours: tuple[int, int] | None, local_hour: int) -> bool:
    """`(8, 23)` allows 08:00-22:59; `(22, 6)` wraps midnight; None is always."""
    if hours is None:
        return True
    start, end = hours
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end


class Throttle:
    def __init__(
        self,
        policy: Policy,
        store: ThrottleStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or secrets.SystemRandom()

    def check(self) -> None:
        """Raise `ThrottleStop` if the platform may not be touched right now."""
        p, now = self.policy, self.clock()
        local_hour = now.astimezone(ZoneInfo(p.timezone)).hour
        if not in_active_hours(p.active_hours, local_hour):
            raise OutsideActiveHours(f"{p.platform}: outside active hours {p.active_hours}")
        block = self.store.get_block(p.platform)
        if block.blocked_until is not None and block.blocked_until > now:
            raise CircuitOpen(
                f"{p.platform}: paused until {block.blocked_until:%Y-%m-%d %H:%M}Z ({block.reason})"
            )
        if self.store.count_since(p.platform, now - timedelta(hours=1)) >= p.hourly_budget:
            raise BudgetExhausted(f"{p.platform}: hourly budget {p.hourly_budget} used")
        if self.store.count_since(p.platform, now - timedelta(days=1)) >= p.daily_budget:
            raise BudgetExhausted(f"{p.platform}: daily budget {p.daily_budget} used")
        self._check_deadline(now)

    def _check_deadline(self, start: datetime) -> None:
        """Raise `OutOfTime` if a page load starting at `start` would start too late."""
        p = self.policy
        if p.deadline is not None and start > p.deadline:
            raise OutOfTime(
                f"{p.platform}: out of cycle time -- no page load starts after "
                f"{p.deadline:%H:%M:%S}Z, so this run ends before the next is due"
            )

    def acquire(self) -> None:
        """Block until the next request is allowed, or raise `ThrottleStop`."""
        self.check()
        last = self.store.last_request_at(self.policy.platform)
        gap = self.rng.uniform(self.policy.min_delay_s, self.policy.max_delay_s)
        wait = 0.0 if last is None else gap - (self.clock() - last).total_seconds()
        # Before sleeping: a gap that would carry the load past the deadline is not waited.
        self._check_deadline(self.clock() + timedelta(seconds=max(wait, 0.0)))
        if wait > 0:
            self.sleep(wait)

    def dwell(self, lo: float = 4.0, hi: float = 12.0) -> None:
        """A human pause inside a page (between scrolls). Not counted as a request."""
        self.sleep(self.rng.uniform(lo, hi))

    def failing(self) -> bool:
        """Whether the last `ERROR_STREAK` loads on this platform all failed -- across
        runs, from the breaker's persisted count -- so the breaker has paused it and a
        crawler ends its run."""
        return self.store.get_block(self.policy.platform).consecutive_failures >= ERROR_STREAK

    def notice(self, outcome: str) -> bool:
        """Whether a failed load just recorded as `outcome` is a notice, not an error, for
        an anonymous crawl: a timeout or a server error up to and including the one that
        made the platform `failing` -- the pause is the response, and only a failure on
        the first load after it is an outage that needs a person -- or a challenge
        before `CHALLENGE_STREAK` -- counted across runs, from the breaker's persisted
        streak -- or a rate limit after a good load whose pause is short of the cap. A
        logged-in crawl (X) reads `failing` itself and asks this of a rate limit only: a
        challenge there is an account a person has to unlock, and waiting a cooldown to
        say so loses a day."""
        p = self.policy.platform
        failures = self.store.get_block(p).consecutive_failures
        if outcome in TRANSIENT:
            return failures <= ERROR_STREAK
        if outcome == CHALLENGED:
            return failures < CHALLENGE_STREAK
        if outcome == RATE_LIMITED:
            # The load before this one succeeded, so the last pause worked, and the pause
            # just set is not yet the longest the breaker has.
            return failures <= 1 and self._backoff_minutes(failures) < (
                self.policy.backoff_max_minutes
            )
        return False

    def _backoff_minutes(self, failures: int) -> float:
        """The pause for a rate limit that brings the breaker's streak to `failures`.

        It doubles with the streak or with the failed loads in `RATE_LIMIT_WINDOW`,
        whichever is more: a success between limits resets the streak, and on
        2026-10-23 X was paused 30 minutes for a limit at 15:38 and 30 again for one at
        17:05, after one clean cycle, so a pace X had begun refusing never slowed.
        """
        p = self.policy
        since = self.clock() - RATE_LIMIT_WINDOW
        steps = max(failures, self.store.failures_since(p.platform, since))
        return min(p.backoff_base_minutes * 2 ** (steps - 1), p.backoff_max_minutes)

    def record(self, outcome: str, kind: str, status: int | None = None, detail: str = "") -> None:
        """Log the request and move the circuit breaker according to `outcome`."""
        p, now = self.policy, self.clock()
        self.store.log_request(p.platform, now, kind, status, outcome == OK)
        if outcome == LOGIN_REQUIRED:
            return
        block = self.store.get_block(p.platform)
        if outcome == OK:
            if block.consecutive_failures or block.blocked_until:
                self.store.set_block(p.platform, Block(None, None, 0), now)
            return
        failures = block.consecutive_failures + 1
        until: datetime | None
        if outcome == CHALLENGED:
            hours = min(p.block_cooldown_hours * 2 ** (failures - 1), p.block_cooldown_max_hours)
            until = now + timedelta(hours=hours)
        elif outcome == RATE_LIMITED:
            until = now + timedelta(minutes=self._backoff_minutes(failures))
        else:
            # A plain error (timeout, 5xx) only pauses once it keeps happening.
            until = (
                now + timedelta(minutes=p.backoff_base_minutes)
                if failures >= ERROR_STREAK
                else None
            )
        reason = f"{outcome}: {detail}"[:500]
        self.store.set_block(p.platform, Block(until, reason, failures), now)
        if until is not None:
            log.warning("%s paused until %s -- %s", p.platform, until.isoformat(), reason)
