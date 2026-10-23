"""What every feed crawler shares: one throttled load, what a failed one means, cursors.

A *feed* is one page a platform serves for one subject -- a symbol's StockTwits stream
or TradingView ideas, a Bluesky search, a forum's thread list -- read newest first and
stopped at the cursor. `FeedCrawler` owns the parts that are the same on every one of
them, with Reddit's semantics (`reddit.scraper`), since those are the ones the breaker
and the run report were built around:

- a **challenge** or **rate limit** ends the run: the throttle has recorded the pause,
  and the next load would only knock on the same door. A rate limit is a notice while
  the breaker's backoff can still answer it, and an error once it cannot; a challenge is a
  notice until it comes again after the cooldown (`Throttle.notice`), since every feed
  is read anonymously and only waiting lifts the block;
- a **timeout** is a notice until the platform keeps failing (`Throttle.failing`);
- any other failure, or a page that does not parse, is an error for that subject, and
  the run goes on to the next;
- the cycle's **deadline** (`OutOfTime`) ends the run with what it read, as a notice.

`pick_symbols` is the stock domain's choice of subjects, shared by the symbol feeds: the
watchlist, then whatever is trending across every platform, each at most once per
`min_gap` on a platform.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.engine import Engine

from social_scraper.db import repository
from social_scraper.fetch import Browser, FetchFailed, PageResult
from social_scraper.pipeline import IngestStats, PostIngestor
from social_scraper.throttle import (
    CHALLENGED,
    ERROR,
    OK,
    RATE_LIMITED,
    TIMED_OUT,
    OutOfTime,
    Throttle,
)
from social_scraper.x import discovery

log = logging.getLogger(__name__)

TRENDING_WINDOW = timedelta(hours=24)
TRENDING_LIMIT = 30
# What a Cloudflare (or similar) interstitial says instead of the page. Matched against
# the visible text only, so a post that quotes one of these is not a wall.
WALL_MARKERS = (
    "captcha",
    "verify you are human",
    "checking your browser",
    "checking if the site connection is secure",
    "enable javascript and cookies to continue",
    "attention required!",
    "just a moment...",
)

JSON = "json"
HTML = "html"


@dataclass
class FeedRunStats:
    """One platform's run, in the shape the run report expects of every crawl."""

    requests: int = 0
    ingest: IngestStats = field(default_factory=IngestStats)
    errors: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


def classify(
    result: PageResult, expect: str = JSON, walls: Sequence[str] = WALL_MARKERS
) -> tuple[str, str]:
    """What a page load means for the throttle: `(outcome, detail)`.

    `expect` is what a good answer carries: parsed JSON (`JSON`) or a document (`HTML`).
    A wall is only looked for when the answer is not what was expected -- a JSON body
    never is one, whatever its text says.
    """
    status = result.status
    if result.timed_out:
        return TIMED_OUT, "page load timed out"
    if status == 429:
        return RATE_LIMITED, "HTTP 429"
    have = result.data is not None if expect == JSON else bool(result.html)
    text = result.text.lower()
    if not have or status in (401, 403, 503):
        if any(marker in text for marker in walls):
            return CHALLENGED, f"challenge page at {result.final_url}"
    if status in (401, 403):
        return CHALLENGED, f"HTTP {status} (network or account block)"
    if status is None or status >= 500:
        return ERROR, f"HTTP {status}"
    if status == 404:
        return OK, "not found"
    if status >= 400:
        return ERROR, f"HTTP {status}"
    if not have:
        return ERROR, no_body(result, expect)
    return OK, ""


def no_body(result: PageResult, expect: str) -> str:
    """Why a load that answered had nothing to read, with what an API said instead
    ("response larger than ..."). A page's visible text is not added: it says nothing
    a challenge check has not already read."""
    why = f" ({result.text[:120]})" if result.text and expect == JSON else ""
    return f"HTTP {result.status} but no {expect} body{why}"


def pick_symbols(
    engine: Engine,
    prefix: str,
    candidates: Sequence[str],
    n: int,
    now: datetime,
    min_gap: timedelta,
) -> list[str]:
    """`n` symbols to read this cycle, stamped as read: `candidates` first (the
    watchlist, a platform's own trending list), then the symbols trending everywhere.

    The stamp is the state key `prefix + symbol`, so each platform paces its own.
    """
    if n <= 0:
        return []
    with engine.connect() as conn:
        trending = [
            s for s, _ in repository.trending_symbols(conn, now - TRENDING_WINDOW, TRENDING_LIMIT)
        ]
        last = {
            k: datetime.fromisoformat(v)
            for k, v in repository.states_with_prefix(conn, prefix).items()
        }
    picked = discovery.pick_symbols([*candidates, *trending], last, now, n, min_gap)
    with engine.begin() as conn:
        for symbol in picked:
            repository.set_state(conn, prefix + symbol, now.isoformat(), now)
    return picked


class FeedCrawler:
    """Throttled loads and their failure semantics; a platform supplies `crawl`."""

    platform = ""

    def __init__(
        self,
        browser: Browser,
        throttle: Throttle,
        ingestor: PostIngestor,
        engine: Engine,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.browser = browser
        self.throttle = throttle
        self.ingestor = ingestor
        self.engine = engine
        self.clock = clock
        self.stats = FeedRunStats()

    def run_steps(self, steps: Sequence[tuple[str, Callable[[], None]]]) -> FeedRunStats:
        """Run each `(label, step)` in turn under the failure rules in the module docstring."""
        try:
            for label, step in steps:
                self.guarded(label, step)
        except OutOfTime as exc:
            self.stats.notices.append(str(exc))
        except FetchFailed as exc:
            log.warning("%s run stopped: %s", self.platform, exc)
        return self.stats

    def guarded(self, label: str, step: Callable[[], None]) -> None:
        try:
            step()
        except FetchFailed as exc:
            noticed = self.failed(f"{label}: {exc}", exc.outcome)
            if exc.outcome in (CHALLENGED, RATE_LIMITED) or (
                exc.outcome == TIMED_OUT and not noticed
            ):
                raise
        except ValueError as exc:
            self.stats.errors.append(f"{label}: unparseable page ({exc})")

    def failed(self, line: str, outcome: str) -> bool:
        """Record a failed load: a notice when it timed out and the platform is not
        failing, was a first challenge, or a rate limit the backoff still answers
        (`Throttle.notice`), else an error. True when
        it was a notice."""
        if self.throttle.notice(outcome):
            self.stats.notices.append(line)
            return True
        self.stats.errors.append(line)
        return False

    def fetch(self, url: str, kind: str, expect: str = JSON) -> PageResult:
        """One throttled load. Raises `FetchFailed` unless it returned content (or a 404,
        which a caller reads as "nothing here")."""
        self.throttle.acquire()
        load = self.browser.get_json if expect == JSON else self.browser.get_html
        result = load(url)
        self.stats.requests += 1
        outcome, detail = self.classify(result, expect)
        self.throttle.record(outcome, kind, result.status, detail)
        if outcome != OK:
            raise FetchFailed(outcome, f"{detail} <- {url}")
        return result

    def classify(self, result: PageResult, expect: str) -> tuple[str, str]:
        return classify(result, expect)

    def ingest(self, posts: Sequence) -> IngestStats:
        stats = self.ingestor.ingest(posts)
        self.stats.ingest.add(stats)
        return stats

    def get_state(self, key: str) -> str | None:
        with self.engine.connect() as conn:
            return repository.get_state(conn, key)

    def set_state(self, key: str, value: str) -> None:
        now = self.clock()
        with self.engine.begin() as conn:
            repository.set_state(conn, key, value, now)
