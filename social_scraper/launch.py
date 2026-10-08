"""A browser that would not start: a skipped platform for a cycle or two, then an error.

Chrome's launch has Playwright's own timeout (180 s). It ran out on 2026-10-07 16:33Z,
in the hour after a boot, and again on 2026-10-08 03:56Z, with a Reddit backfill
decompressing a 350-million-line dump and the Docker engine wedged beside it -- and
each time the crawl reported it as an error, so the whole scheduled scrape exited 1.
A launch that misses its timeout on a loaded machine is not the scraper broken: the
next launch is the retry. So it skips the platform's crawl, and only `LAUNCH_STREAK`
launches in a row that could not start it are an error -- a profile another Chrome
holds, a broken install -- which the persisted streak keeps visible across cycles.

Both domains' `run_platform`s (`runner`, `sports.cycle`) share this, keyed per platform,
since each platform has one profile whichever domain launched it: two launches a cycle,
so a machine that cannot start Chrome is an error in its second cycle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.engine import Engine

from social_scraper.db import repository

LAUNCH_STREAK = 3


def state_key(platform: str) -> str:
    return f"browser:{platform}:launch_timeouts"


def timed_out(engine: Engine, platform: str, exc: Exception, now: datetime) -> dict[str, Any]:
    """The crawl's result for a launch that timed out: a skip, or an error at the streak."""
    key = state_key(platform)
    with engine.begin() as conn:
        streak = int(repository.get_state(conn, key) or 0) + 1
        repository.set_state(conn, key, str(streak), now)
    if streak < LAUNCH_STREAK:
        return {"skipped": f"browser did not start ({streak} launch(es) in a row): {exc}"}
    return {"error": f"browser did not start in {streak} launches in a row: {exc}"}


def started(engine: Engine, platform: str, now: datetime) -> None:
    """The browser started: the streak ends, writing nothing when there was none."""
    key = state_key(platform)
    with engine.begin() as conn:
        if repository.get_state(conn, key) not in (None, "0"):
            repository.set_state(conn, key, "0", now)
