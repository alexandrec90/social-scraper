"""Incremental subreddit crawl through the browser.

Per subreddit, each cycle:

1. `new.json` -- every new submission, paging back until a post already seen.
2. `comments.json` -- the subreddit-wide newest-comments stream. One request returns
   100 comments across every thread, which is where most of the sentiment on
   r/wallstreetbets lives (the daily threads), at a fraction of the cost of opening
   threads one by one.
3. `top.json?t=day`, every `refresh_hours` -- re-observes the day's posts so their
   engagement snapshots show how they matured, not just the score at minute one.

The newest `created_utc` seen per listing is the cursor, persisted in `crawl_state`.
"""

from __future__ import annotations

import logging
import random
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from sqlalchemy.engine import Engine

from social_scraper.db import repository
from social_scraper.fetch import Browser, FetchFailed, PageResult
from social_scraper.pipeline import IngestStats, PostIngestor
from social_scraper.reddit.parse import REDDIT_BASE, parse_listing
from social_scraper.throttle import (
    CHALLENGED,
    ERROR,
    OK,
    RATE_LIMITED,
    TIMED_OUT,
    OutOfTime,
    Throttle,
)

log = logging.getLogger(__name__)

PAGE_SIZE = 100
_WALL_MARKERS = ("captcha", "prove your humanity", "verify you are human", "whoa there")
# A fresh profile's first page load is a JavaScript challenge that renders "blocked by
# network security" with HTTP 200; the next load passes and Reddit issues the anonymous
# session (`loid`, `token_v2`) the JSON endpoints require. Measured 2026-09-28.
WARM_UP_ATTEMPTS = 3


def is_js_challenge(result: PageResult) -> bool:
    """The interstitial a new browser gets before Reddit trusts it with a session."""
    return "js_challenge=1" in result.final_url or (
        result.status == 200 and "blocked by network security" in result.text.lower()
    )


def listing_url(sub: str, listing: str, after: str | None = None, extra: str = "") -> str:
    url = f"{REDDIT_BASE}/r/{quote(sub)}/{listing}.json?limit={PAGE_SIZE}&raw_json=1"
    if extra:
        url += f"&{extra}"
    if after:
        url += f"&after={quote(after)}"
    return url


def classify(result: PageResult, expect_json: bool = True) -> tuple[str, str]:
    """What a Reddit page load means for the throttle: `(outcome, detail)`."""
    status, text = result.status, result.text.lower()
    if result.timed_out:
        return TIMED_OUT, "page load timed out"
    if status == 429:
        return RATE_LIMITED, "HTTP 429"
    if any(marker in text for marker in _WALL_MARKERS):
        return CHALLENGED, f"challenge page at {result.final_url}"
    if status in (401, 403):
        return CHALLENGED, f"HTTP {status} (network or account block)"
    if status is None or status >= 500:
        return ERROR, f"HTTP {status}"
    if status == 404:
        return OK, "not found (banned, private or renamed subreddit)"
    if expect_json and result.data is None:
        return ERROR, f"HTTP {status} but no JSON body"
    return OK, ""


def _no_json(_url: str) -> bool:
    return False


def _no_pause() -> None:
    return None


@dataclass
class RedditRunStats:
    requests: int = 0
    ingest: IngestStats = field(default_factory=IngestStats)
    errors: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RedditOptions:
    max_pages: int = 3
    refresh_hours: float = 6.0
    ignore_authors: frozenset[str] = frozenset()


class RedditScraper:
    def __init__(
        self,
        browser: Browser,
        throttle: Throttle,
        ingestor: PostIngestor,
        engine: Engine,
        options: RedditOptions | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        options = options or RedditOptions()
        self.browser = browser
        self.throttle = throttle
        self.ingestor = ingestor
        self.engine = engine
        self.max_pages = options.max_pages
        self.refresh_hours = options.refresh_hours
        self.ignore_authors = options.ignore_authors
        self.clock = clock
        self.stats = RedditRunStats()

    def run(
        self, subreddits: tuple[str, ...], rng: random.Random | None = None, warm: bool = True
    ) -> RedditRunStats:
        """Crawl every subreddit once, in random order.

        `ThrottleStop` propagates (the platform is paused or out of budget), except the
        cycle's deadline, which ends the run with what it crawled and a notice. A wall or
        rate limit ends the run early; the throttle has already recorded the pause.
        `warm=False` skips the warm-up, for a browser an earlier run already warmed.
        """
        order = list(subreddits)
        if not order:
            return self.stats
        (rng or secrets.SystemRandom()).shuffle(order)
        try:
            if warm:
                self.warm_up(order[0])
            for sub in order:
                for listing in ("new", "comments"):
                    self._guarded(sub, listing)
                if self._refresh_due(sub):
                    self._guarded(sub, "top", extra="t=day", pages=1, incremental=False)
        except OutOfTime as exc:
            self.stats.notices.append(str(exc))
        except FetchFailed as exc:
            log.warning("reddit run stopped: %s", exc)
        return self.stats

    def warm_up(self, sub: str) -> None:
        """Load a real subreddit page before any JSON, as a person arriving would.

        Measured, not guessed: a profile without a Reddit session gets a 403 from every
        JSON endpoint. Its first HTML load is a JavaScript challenge (`is_js_challenge`);
        the next load passes and establishes the session. So load the subreddit page
        until it is not the challenge, up to `WARM_UP_ATTEMPTS` times, each throttled
        and counted like any other request. A profile that already has a session passes
        on the first load.
        """
        url = f"{REDDIT_BASE}/r/{quote(sub)}/"
        for _ in range(WARM_UP_ATTEMPTS):
            self.throttle.acquire()
            result = self.browser.capture(url, _no_json, 0, _no_pause)
            self.stats.requests += 1
            if is_js_challenge(result):
                # Expected once per new profile; not a block, so the breaker is untouched.
                self.throttle.record(OK, "warm_up:js_challenge", result.status)
                continue
            outcome, detail = classify(result, expect_json=False)
            self.throttle.record(outcome, "warm_up", result.status, detail)
            if outcome == OK:
                return
            self._failed(f"warm-up r/{sub}: {detail}", outcome)
            raise FetchFailed(outcome, detail)
        detail = f"JavaScript challenge not cleared after {WARM_UP_ATTEMPTS} loads"
        self.throttle.record(CHALLENGED, "warm_up", None, detail)
        self._failed(f"warm-up r/{sub}: {detail}", CHALLENGED)
        raise FetchFailed(CHALLENGED, detail)

    def _guarded(self, sub: str, listing: str, **kwargs) -> None:
        try:
            self.crawl_listing(sub, listing, **kwargs)
        except FetchFailed as exc:
            noticed = self._failed(f"r/{sub}/{listing}: {exc}", exc.outcome)
            # A timeout streak stops the run here: the breaker has just paused the
            # platform, and the next `acquire` would end it as a skip, losing this run.
            if exc.outcome in (CHALLENGED, RATE_LIMITED) or (
                exc.outcome == TIMED_OUT and not noticed
            ):
                raise
        except ValueError as exc:
            self.stats.errors.append(f"r/{sub}/{listing}: unparseable page ({exc})")

    def _failed(self, line: str, outcome: str) -> bool:
        """Record a failed load: a notice when it timed out and the platform is not
        failing, or was a first challenge (`Throttle.notice`), else an error. True when
        it was a notice."""
        if self.throttle.notice(outcome):
            self.stats.notices.append(line)
            return True
        self.stats.errors.append(line)
        return False

    def crawl_listing(
        self,
        sub: str,
        listing: str,
        *,
        extra: str = "",
        pages: int | None = None,
        incremental: bool = True,
    ) -> IngestStats:
        """Page through one listing, newest first, until reaching what was already seen."""
        key = f"reddit:{sub}:{listing}"
        with self.engine.connect() as conn:
            seen_through = float(repository.get_state(conn, key) or 0)
        newest, after, stats = seen_through, None, IngestStats()
        for _ in range(pages or self.max_pages):
            result = self.fetch(listing_url(sub, listing, after, extra), f"listing:{listing}")
            if result.status == 404:
                break
            posts, after = parse_listing(result.data, self.ignore_authors)
            stats.add(self.ingestor.ingest(posts))
            stamps = [p.created_at.timestamp() for p in posts]
            newest = max([newest, *stamps])
            if not after or not stamps or (incremental and min(stamps) <= seen_through):
                break
        now = self.clock()
        with self.engine.begin() as conn:
            if incremental:
                repository.set_state(conn, key, str(newest), now)
            else:
                repository.set_state(conn, f"reddit:{sub}:refresh", now.isoformat(), now)
        self.stats.ingest.add(stats)
        return stats

    def fetch(self, url: str, kind: str) -> PageResult:
        """One throttled page load. Raises `FetchFailed` unless it returned content."""
        self.throttle.acquire()
        result = self.browser.get_json(url)
        self.stats.requests += 1
        outcome, detail = classify(result)
        self.throttle.record(outcome, kind, result.status, detail)
        if outcome != OK:
            raise FetchFailed(outcome, f"{detail} <- {url}")
        return result

    def _refresh_due(self, sub: str) -> bool:
        with self.engine.connect() as conn:
            last = repository.get_state(conn, f"reddit:{sub}:refresh")
        if last is None:
            return True
        return self.clock() - datetime.fromisoformat(last) >= timedelta(hours=self.refresh_hours)
