"""Pacing that keeps the accounts from getting banned.

Every page load goes through `Throttle.acquire()` first and reports back through
`Throttle.record()`. What it enforces, per platform:

- **Spacing** -- a uniform-random gap in `[min_delay, max_delay]` since the previous
  request. Jitter matters: a fixed interval is the easiest bot signature there is.
- **Budgets** -- at most N requests per rolling hour and per rolling day, counted from
  the persisted request log, so restarting the process does not reset them.
- **Active hours** -- outside the window the platform is simply skipped.
- **Circuit breaker** -- a rate-limit response backs off exponentially
  (`backoff_base * 2^failures`, capped); a challenge (captcha, login wall, locked
  account) opens the circuit for `block_cooldown` and needs no further failures to do
  so. A success closes it.

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
    active_hours: tuple[int, int] | None = None
    timezone: str = "UTC"


@dataclass
class Block:
    blocked_until: datetime | None
    reason: str | None
    consecutive_failures: int


class ThrottleStore(Protocol):
    def count_since(self, platform: str, since: datetime) -> int: ...
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

    def acquire(self) -> None:
        """Block until the next request is allowed, or raise `ThrottleStop`."""
        self.check()
        last = self.store.last_request_at(self.policy.platform)
        gap = self.rng.uniform(self.policy.min_delay_s, self.policy.max_delay_s)
        if last is not None:
            wait = gap - (self.clock() - last).total_seconds()
            if wait > 0:
                self.sleep(wait)

    def dwell(self, lo: float = 2.0, hi: float = 6.0) -> None:
        """A human pause inside a page (between scrolls). Not counted as a request."""
        self.sleep(self.rng.uniform(lo, hi))

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
            until = now + timedelta(hours=p.block_cooldown_hours)
        elif outcome == RATE_LIMITED:
            minutes = min(p.backoff_base_minutes * 2 ** (failures - 1), p.backoff_max_minutes)
            until = now + timedelta(minutes=minutes)
        else:
            # A plain error (timeout, 5xx) only pauses once it keeps happening.
            until = now + timedelta(minutes=p.backoff_base_minutes) if failures >= 3 else None
        reason = f"{outcome}: {detail}"[:500]
        self.store.set_block(p.platform, Block(until, reason, failures), now)
        if until is not None:
            log.warning("%s paused until %s -- %s", p.platform, until.isoformat(), reason)
